import logging
import re
import os
import shutil
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import yt_dlp

from app.config import settings

logger = logging.getLogger("media_api")

def _configure_js_runtime(ydl_opts: Dict[str, Any]) -> None:
    """Enables the node runtime for YouTube JS challenges; the solver itself comes from the yt-dlp-ejs package."""
    if shutil.which("node"):
        ydl_opts["js_runtimes"] = {"node": {}}

QUALITY_HEIGHTS = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

# --- X (Twitter) -------------------------------------------------------------
# Public X posts are reachable with yt-dlp's guest token, so downloading from X
# needs no account, no cookies and no API key. That is deliberate and enforced
# in _base_opts: an X request must never be able to carry the user's session.

X_HOSTS = frozenset({
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "t.co", "www.t.co",
    "amp.twimg.com",
})

X_SINGLE = "single"
X_PHOTO = "photo"
X_UNSUPPORTED = "unsupported"

# Mirrors the _VALID_URL patterns of yt-dlp's Twitter extractors. Anything on an
# X host that matches none of them has no extractor at all: X exposes no public
# timeline, so profiles, search, lists and bookmarks cannot be read or expanded.
_X_SINGLE_PATTERNS = (
    re.compile(r"^/(?:i/web|[^/]+)/status/\d+(?:/video/\d+)?/?$"),
    re.compile(r"^/statuses/\d+(?:/video/\d+)?/?$"),
    re.compile(r"^/i/videos(?:/tweet)?/\d+/?$"),
    re.compile(r"^/i/cards/tfw/v1/\d+/?$"),
    re.compile(r"^/i/(?:broadcasts|events)/\w+/?$"),
    re.compile(r"^/i/spaces/[0-9a-zA-Z]{13}"),
    re.compile(r"^/v/[0-9a-f-]{36}/?$"),  # amp.twimg.com
)

# A /photo/N post is a still image, so it is worth its own message rather than
# the generic "no extractor" one.
_X_PHOTO_PATTERN = re.compile(r"^/(?:i/web|[^/]+)/status/\d+/photo/\d+/?$")


def _parse(url: str) -> Optional[urllib.parse.ParseResult]:
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return None
    try:
        return urllib.parse.urlparse(url.strip())
    except Exception:
        return None


def is_x_host(url: str) -> bool:
    """True for any URL on an X/Twitter host, whatever its path."""
    parsed = _parse(url)
    if not parsed:
        return False
    return (parsed.hostname or "").lower() in X_HOSTS


def x_url_kind(url: str) -> Optional[str]:
    """
    Classify an X URL without touching the network.

    Returns None when the URL is not on an X host, so callers can treat
    "not X" and "X but unusable" as the distinct cases they are.
    """
    parsed = _parse(url)
    if not parsed:
        return None
    host = (parsed.hostname or "").lower()
    if host not in X_HOSTS:
        return None
    # t.co is a shortener: the path is an opaque key, so it cannot be classified
    # here. yt-dlp resolves it and the real extractor decides.
    if host in ("t.co", "www.t.co"):
        return X_SINGLE
    # Lowercased so a pasted /STATUS/ or /Video/ still classifies; every pattern
    # below is lowercase, and the ids in them are digits or hex.
    path = (parsed.path or "/").lower()
    if _X_PHOTO_PATTERN.match(path):
        return X_PHOTO
    if any(pattern.match(path) for pattern in _X_SINGLE_PATTERNS):
        return X_SINGLE
    return X_UNSUPPORTED


X_TIMELINE_MESSAGE = (
    "Only individual X posts can be downloaded. X offers no public access to profiles, "
    "timelines, search, lists or bookmarks, so there is nothing to read from this URL. "
    "Open the specific post and use its link, which looks like "
    "https://x.com/<account>/status/<numbers>."
)

X_PHOTO_MESSAGE = (
    "That X link points at a photo, which has no video or audio to transcribe. If the "
    "post also contains a video, use the post's own link without the /photo/N ending."
)


def x_refusal_message(kind: Optional[str]) -> Optional[str]:
    """The message for an X URL that cannot be used, or None if it can."""
    if kind == X_PHOTO:
        return X_PHOTO_MESSAGE
    if kind == X_UNSUPPORTED:
        return X_TIMELINE_MESSAGE
    return None


def x_friendly_error(url: str, exc: Exception) -> Optional[str]:
    """
    Turn the X failures a user can act on into a readable sentence.

    yt-dlp's own text ("ERROR: [twitter] 123: Unsupported URL") says nothing
    about what to do instead, and these three cases are the ones that actually
    come up. Returns None when there is nothing better to say, so the caller
    keeps the original message rather than inventing one.
    """
    if not is_x_host(url):
        return None
    text = str(exc).lower()
    if "no video could be found" in text or "no media could be found" in text:
        return (
            "That X post has no video in it. It may be text or images only, or the video "
            "may have been removed since the link was made."
        )
    if "unsupported url" in text:
        return X_TIMELINE_MESSAGE
    if any(marker in text for marker in ("nsfw", "age-restricted", "log in", "logged in", "login")):
        return (
            "That X post needs a signed-in account to view, so it cannot be downloaded: "
            "media-api never signs in to X. Age-restricted, protected and sensitive posts "
            "are out of reach by design."
        )
    return None


def _video_format_selector(quality: str, url: str = "") -> str:
    """yt-dlp format string: the best streams, capped at the requested height ("best" = no cap)."""
    height = QUALITY_HEIGHTS.get(quality)
    if is_x_host(url):
        # X tops out around 720p and serves progressive MP4s at the same heights
        # as its HLS renditions. Preferring the progressive stream avoids an
        # ffmpeg merge, keeps download progress a single 0-100% pass, and stays
        # cancellable between fragments.
        if height is None:
            return "b[protocol^=http]/bv*+ba/b"
        return (
            f"b[protocol^=http][height<={height}]/b[height<={height}]"
            f"/bv*[height<={height}]+ba/bv*+ba/b"
        )
    if height is None:
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"

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

    # The markers below are YouTube-shaped and would misread X paths: a post by
    # the handle @c is /c/status/<id>, which contains "/c/". X URLs are
    # classified by x_url_kind() instead, so they never reach the batch gate.
    if is_x_host(url):
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


def _base_opts(url: str = "") -> Dict[str, Any]:
    """
    The yt-dlp options every call shares, including the cookie policy.

    A cookiefile is attached for every host except X. Public X posts extract with
    a guest token, so an X request never needs the user's session -- and must
    never carry it, even once a cookies.txt exists in the cache dir for some
    other site. Options that differ per call site (skip_download, extract_flat,
    socket_timeout, playlistend, and everything download() sets) stay with their
    caller on purpose.
    """
    opts: Dict[str, Any] = {"quiet": True, "no_warnings": True}
    _configure_js_runtime(opts)
    if not is_x_host(url):
        cookiefile = _get_cookiefile()
        if cookiefile:
            opts["cookiefile"] = cookiefile
    return opts

class YtDlpService:
    @staticmethod
    def extract_info(url: str) -> Dict[str, Any]:
        ydl_opts = {
            **_base_opts(url),
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def probe_url(url: str) -> Dict[str, Any]:
        ydl_opts = {
            **_base_opts(url),
            "skip_download": True,
            "extract_flat": True,
            "socket_timeout": 6,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Could not extract metadata from URL")

            # An X post holding several videos extracts as a playlist, whose own
            # duration and thumbnail are empty. Preview the first video and
            # report the count so the caller can offer a picker.
            #
            # Scoped to X on purpose: this call sets no `noplaylist`, so a
            # YouTube playlist URL also arrives with entries, and collapsing
            # those would change the preview card for every playlist and claim a
            # picker on the Download tab, which has no batch mode.
            video_count = 1
            if is_x_host(url):
                entries = [entry for entry in (info.get("entries") or []) if entry]
                video_count = info.get("playlist_count") or len(entries) or 1
                if entries:
                    info = {**entries[0], "title": info.get("title") or entries[0].get("title")}

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
                "video_count": video_count,
            }

    @staticmethod
    def download(
        url: str,
        audio_only: bool = False,
        target_format: str = "mp4",
        progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        custom_out_dir: Optional[Path] = None,
        quality: str = "best",
        playlist_index: Optional[int] = None
    ) -> Dict[str, Any]:
        out_dir = custom_out_dir or (settings.output_audio if audio_only else settings.output_videos)
        out_dir.mkdir(parents=True, exist_ok=True)

        out_tmpl = str(out_dir / "%(title)s_%(id)s.%(ext)s")

        ydl_opts = {
            **_base_opts(url),
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "nocheckcertificate": True,
            "restrictfilenames": True,
        }

        # An X post with several videos extracts as a playlist that `noplaylist`
        # does not collapse, so a specific video is chosen by its position in the
        # extracted entries. That is the list the batch picker shows, which is
        # why the position is passed through rather than a /video/N URL suffix:
        # /video/N indexes the post's media *including photos*, so the two
        # disagree the moment a post mixes images and video.
        if playlist_index is not None:
            ydl_opts["playlist_items"] = str(int(playlist_index))

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
            ydl_opts["format"] = _video_format_selector(quality, url)
            if target_format == "webm":
                ydl_opts["merge_output_format"] = "webm/mkv"
            else:
                # The file must come out as .mp4, as selected. Resolution still
                # wins; at a given resolution prefer MP4/M4A streams (H.264/AV1 +
                # AAC) so the merge is a plain copy that plays everywhere. When
                # the top resolution exists only as VP9/Opus, ffmpeg copies those
                # into MP4 too, so "mp4/mkv" no longer silently yields an .mkv.
                ydl_opts["format_sort"] = ["res", "fps", "ext:mp4:m4a"]
                ydl_opts["merge_output_format"] = "mp4"
                # A single (unmerged) .webm stream is remuxed rather than kept.
                ydl_opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # A playlist result carries no `requested_downloads` of its own, so
            # read the downloaded entry instead of falling through to
            # prepare_filename(), which would name a file nothing ever wrote.
            entries = [entry for entry in (info.get("entries") or []) if entry]
            # playlist_count is the post's real total, which still holds when
            # playlist_items narrowed the entries to the one that was picked.
            total_videos = info.get("playlist_count") or len(entries) or 1
            selected = entries[0] if entries else info

            downloads = selected.get("requested_downloads") or [{}]
            filename = downloads[0].get("filepath") or ydl.prepare_filename(selected)

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
                "title": selected.get("title") or info.get("title") or "video",
                "duration": selected.get("duration") or 0,
                "uploader": selected.get("uploader") or info.get("uploader") or "Unknown",
                "file_path": filename,
                "url": url,
                "total_videos": total_videos,
            }

    @staticmethod
    def extract_playlist(url: str, max_items: int = 50) -> Dict[str, Any]:
        """
        Fast flat extraction of playlist items or single video metadata.
        Returns a dict with title, uploader, total_items, and items list.
        """
        ydl_opts = {
            **_base_opts(url),
            "skip_download": True,
            "extract_flat": True,
            "playlistend": max_items,
            "socket_timeout": 8,
        }
        is_x = is_x_host(url)

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
                for position, entry in enumerate(entries, 1):
                    if not entry:
                        continue
                    # X entries carry no title when the post has no text, and
                    # is_unavailable_entry() reads a missing title as "deleted".
                    # That signal only holds for YouTube's flat entries.
                    if not is_x and is_unavailable_entry(entry):
                        skipped_unavailable += 1
                        continue

                    entry_index = None
                    if is_x:
                        # X entries are fully-extracted dicts with no "url" key,
                        # so there is nothing to link to per video. The post URL
                        # plus this position is what identifies one video.
                        entry_url = url
                        entry_index = entry.get("playlist_index") or position
                    else:
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

                    entry_title = entry.get("title")
                    if not entry_title and is_x:
                        entry_title = f"Video {entry_index} of {len(entries)}"

                    items.append({
                        "title": entry_title,
                        "url": entry_url,
                        "duration": duration,
                        "duration_formatted": duration_formatted,
                        "thumbnail": thumb,
                        "uploader": entry.get("uploader") or entry.get("channel") or playlist_uploader,
                        "playlist_index": entry_index,
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
                    "uploader": playlist_uploader,
                    "playlist_index": None,
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
