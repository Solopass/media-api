"""
Shared path allow-lists for every endpoint that reads or writes a file the
caller named.

The API is loopback-only, but a request still must not be able to reach
arbitrary files on the workstation: the roots below are the only places a
caller-supplied path may resolve into.
"""

from pathlib import Path

from app.config import settings

def media_roots() -> list[Path]:
    """
    Streamable media and sidecars.

    Only the uploads subdirectory of the AI cache is included: the cache root
    also holds cookies.txt, agent session history and browser profiles, none of
    which belong on an HTTP endpoint.

    Built per call from settings, like storage_service._scan_roots, so the
    allow-list follows a relocated output or vault directory instead of locking
    the user out of their own files.
    """
    return [
        settings.output_videos.parent,
        settings.output_audio.parent,
        settings.uploads_dir,
        settings.vault_transcripts,
        Path("/mnt/d/AI/Vault"),
        Path("/mnt/d/OBVLT"),
    ]


def note_roots() -> list[Path]:
    """Markdown notes, for reading and for in-place rewrites."""
    return [
        settings.vault_transcripts,
        settings.output_transcripts,
        Path("/mnt/d/AI/Vault"),
        Path("/mnt/d/OBVLT"),
    ]

MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".mp4", ".webm", ".mkv", ".mov", ".avi",
    ".json", ".txt", ".md",
}


def is_path_allowed(target: Path, roots: list[Path]) -> bool:
    """True when target resolves to one of roots or something inside it."""
    try:
        resolved_target = target.resolve()
        for root in roots:
            resolved_root = root.resolve()
            if resolved_target == resolved_root or resolved_target.is_relative_to(resolved_root):
                return True
    except Exception:
        return False
    return False


def is_inside(target: Path, root: Path) -> bool:
    """True when target resolves to something inside a single root."""
    return is_path_allowed(target, [root])
