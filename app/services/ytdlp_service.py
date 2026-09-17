import re
import os
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import yt_dlp

from app.config import settings

def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', "", name)
    s = re.sub(r"\s+", "_", s)
    return s[:60].strip("._")

class YtDlpService:
    @staticmethod
    def extract_info(url: str) -> Dict[str, Any]:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True
        }
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
