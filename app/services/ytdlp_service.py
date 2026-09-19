import logging
import re
import os
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import yt_dlp

from app.config import settings

logger = logging.getLogger("media_api")

# Collection URLs resolve to many videos, so they cannot be ingested as one item.
COLLECTION_PATH_MARKERS = ("/playlist", "/channel/", "/c/", "/user/", "/@")


def is_collection_url(url: str) -> bool:
    """
    True for playlist and channel URLs, which yt-dlp expands into many entries.

    A watch URL carrying both `v=` and `list=` is still one video (noplaylist
    keeps it that way), so it is deliberately not treated as a collection.
    """
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False

    path = (parsed.path or "").lower()
    query = urllib.parse.parse_qs(parsed.query or "")

    if any(marker in path for marker in COLLECTION_PATH_MARKERS):
        return True
    # ?list=... without a specific video is a playlist.
    return "list" in query and "v" not in query


def is_unavailable_entry(entry: Dict[str, Any]) -> bool:
    """
    True for private, deleted or region-blocked playlist entries.

    Flat extraction reports these with every field empty except the id, so the
    absence of a title is the reliable signal. They cannot be downloaded, and
    presenting them as ingestible items just queues guaranteed failures.
    """
    title = (entry.get("title") or "").strip()
    if not title:
        return True
    return title.lower() in (
        "[private video]", "[deleted video]", "[unavailable video]", "[video unavailable]"
    )

def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return s[:60].strip("._")

def _get_cookiefile() -> Optional[str]:
    """Auto-detect cookies.txt in workstation cache directory if present."""
    cookie_path = settings.cache_dir / "cookies.txt"
    if cookie_path.exists():
        return str(cookie_path)
    return None

class YtDlpService:
    @staticmethod
    def extract_info(url: str) -> Dict[str, Any]:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True
        }
        cookiefile = _get_cookiefile()
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def probe_url(url: str) -> Dict[str, Any]:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "socket_timeout": 6,
        }
        cookiefile = _get_cookiefile()
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Could not extract metadata from URL")
            duration = float(info.get("duration") or 0.0)
            mins = int(duration // 60)
            secs = int(duration % 60)
            duration_formatted = f"{mins:02d}:{secs:02d}"
            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails") and isinstance(info["thumbnails"], list):
                try:
                    thumb = info["thumbnails"][-1].get("url")
                except Exception:
                    thumb = None

            return {
                "title": info.get("title") or "Unknown Title",
                "uploader": info.get("uploader") or info.get("channel") or info.get("extractor_key") or "Unknown",
                "duration": duration,
                "duration_formatted": duration_formatted,
                "thumbnail": thumb,
                "description": (info.get("description") or "")[:250] if info.get("description") else None,
            }

    @staticmethod
    def download(
        url: str,
        audio_only: bool = False,
        target_format: str = "mp4",
        progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        custom_out_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        out_dir = custom_out_dir or (settings.output_audio if audio_only else settings.output_videos)
        out_dir.mkdir(parents=True, exist_ok=True)
        
        out_tmpl = str(out_dir / "%(title)s_%(id)s.%(ext)s")

        ydl_opts = {
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True
        }

        cookiefile = _get_cookiefile()
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        if progress_hook:
            ydl_opts["progress_hooks"] = [progress_hook]

        codec = target_format if target_format in ["mp3", "wav", "m4a"] else "mp3"

        if audio_only:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": "192",
            }]
        else:
            if target_format == "mp4":
                ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            else:
                ydl_opts["format"] = "best"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # If audio was converted, filename extension changed
            if audio_only:
                base_path = Path(filename).with_suffix(f".{codec}")
                if base_path.exists():
                    filename = str(base_path)
            elif not Path(filename).exists():
                # If video+audio were merged (e.g. .webm + .m4a -> .mp4), verify target_format
                alt_path = Path(filename).with_suffix(f".{target_format}")
                if alt_path.exists():
                    filename = str(alt_path)

            return {
                "title": info.get("title", "video"),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", "Unknown"),
                "file_path": filename,
                "url": url
            }

    @staticmethod
    def extract_playlist(url: str, max_items: int = 50) -> Dict[str, Any]:
        """
        Fast flat extraction of playlist items or single video metadata.
        Returns a dict with title, uploader, total_items, and items list.
        """
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "playlistend": max_items,
            "socket_timeout": 8,
        }
        cookiefile = _get_cookiefile()
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Could not extract playlist info from URL")

            items = []
            skipped_unavailable = 0
            entries = info.get("entries")
            if entries is not None:
                playlist_title = info.get("title") or "Playlist"
                playlist_uploader = info.get("uploader") or info.get("channel")
                for entry in entries:
                    if not entry:
                        continue
                    if is_unavailable_entry(entry):
                        skipped_unavailable += 1
                        continue
                    video_id = entry.get("id") or ""
                    entry_url = entry.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
                    if not entry_url.startswith("http"):
                        entry_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else entry_url

                    duration = float(entry.get("duration") or 0.0)
                    mins = int(duration // 60)
                    secs = int(duration % 60)
                    duration_formatted = f"{mins:02d}:{secs:02d}"

                    thumb = entry.get("thumbnail")
                    if not thumb and entry.get("thumbnails") and isinstance(entry["thumbnails"], list):
                        try:
                            thumb = entry["thumbnails"][-1].get("url")
                        except Exception:
                            thumb = None

                    items.append({
                        "title": entry.get("title"),
                        "url": entry_url,
                        "duration": duration,
                        "duration_formatted": duration_formatted,
                        "thumbnail": thumb,
                        "uploader": entry.get("uploader") or entry.get("channel") or playlist_uploader
                    })
            else:
                playlist_title = info.get("title") or "Single Video"
                playlist_uploader = info.get("uploader") or info.get("channel")
                duration = float(info.get("duration") or 0.0)
                mins = int(duration // 60)
                secs = int(duration % 60)
                thumb = info.get("thumbnail")
                if not thumb and info.get("thumbnails") and isinstance(info["thumbnails"], list):
                    try:
                        thumb = info["thumbnails"][-1].get("url")
                    except Exception:
                        thumb = None

                items.append({
                    "title": playlist_title,
                    "url": url,
                    "duration": duration,
                    "duration_formatted": f"{mins:02d}:{secs:02d}",
                    "thumbnail": thumb,
                    "uploader": playlist_uploader
                })

            if skipped_unavailable:
                logger.info(
                    f"[batch] Skipped {skipped_unavailable} unavailable entr"
                    f"{'y' if skipped_unavailable == 1 else 'ies'} in {playlist_title}"
                )

            return {
                "title": playlist_title,
                "uploader": playlist_uploader,
                "total_items": len(items),
                "skipped_unavailable": skipped_unavailable,
                "items": items
            }
