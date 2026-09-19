"""
Storage accounting and age-based reclaim for the API's own output directories.

Ingest deletes the source video it produced, but extracted audio, transcript
sidecars and the drag-and-drop uploads cache still accumulate. This service
reports what is on disk per category and reclaims the parts that are safe to
lose, while never touching notes, sidecars, or files an in-flight job needs.
"""

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.config import settings, to_windows_path
from app.models.schemas import JobStatus
from app.paths import is_path_allowed
from app.queue.manager import job_manager

logger = logging.getLogger("media_api.storage")

VIDEOS = "videos"
AUDIO = "audio"
UPLOADS = "uploads"
SIDECARS = "sidecars"
NOTE_MIRRORS = "note_mirrors"

# Only these may be deleted. Sidecars stay because they are tiny and power the
# transcript view, re-summarization and chapter generation; note mirrors stay
# because they are markdown notes.
RECLAIMABLE = (VIDEOS, AUDIO, UPLOADS)

CATEGORY_LABELS = {
    VIDEOS: "Source Videos",
    AUDIO: "Extracted Audio",
    UPLOADS: "Upload Cache",
    SIDECARS: "Transcript Sidecars",
    NOTE_MIRRORS: "Note Mirrors",
}

AGE_BUCKETS = (7, 30, 90)

# Never deleted, whatever category a file was counted under.
PROTECTED_SUFFIXES = (".md",)
PROTECTED_ENDINGS = (".transcript.json",)

_SCAN_CACHE: Dict[str, Any] = {"timestamp": 0.0, "key": None, "data": None}


def _scan_roots() -> List[Tuple[Path, str]]:
    """
    Directories to walk, with the category their files default to.

    Read from settings on every call rather than at import time, so tests can
    redirect the output paths.
    """
    return [
        (settings.output_videos, VIDEOS),
        (settings.output_audio, AUDIO),
        (settings.output_transcripts, NOTE_MIRRORS),
        (settings.uploads_dir, UPLOADS),
    ]


def _classify(path: Path, default_category: str) -> str:
    """Give each file exactly one category, so totals never double-count."""
    name = path.name.lower()
    if name.endswith(".transcript.json") or path.suffix.lower() == ".txt":
        return SIDECARS
    if path.suffix.lower() == ".md":
        return NOTE_MIRRORS
    return default_category


def _is_protected(path: Path) -> bool:
    name = path.name.lower()
    if any(name.endswith(ending) for ending in PROTECTED_ENDINGS):
        return True
    return path.suffix.lower() in PROTECTED_SUFFIXES


def _iter_files() -> Iterator[Tuple[Path, str, Path]]:
    """Yield (path, category, root) for every file under the scanned roots."""
    for root, default_category in _scan_roots():
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                if path.is_file():
                    yield path, _classify(path, default_category), root
        except Exception as e:
            logger.warning(f"[storage] Could not walk {root}: {e}")


def _active_job_paths() -> set:
    """Files referenced by queued or running jobs, which must not be removed."""
    protected = set()
    try:
        for job in job_manager.list_jobs():
            if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                for value in (job.output_files or {}).values():
                    protected.add(str(Path(value)))
    except Exception as e:
        logger.warning(f"[storage] Could not read active jobs: {e}")
    return protected


def scan(max_cache_age: float = 30.0) -> Dict[str, Any]:
    """
    Per-category totals plus drive usage.

    Cached briefly: these walks hit /mnt/d (DrvFs) and must not run on the UI's
    health poll. The cache key includes the scanned directories so a test that
    redirects them never reads another run's numbers.
    """
    key = "|".join(str(root) for root, _ in _scan_roots())
    now = time.time()
    if (
        _SCAN_CACHE["data"]
        and _SCAN_CACHE["key"] == key
        and (now - _SCAN_CACHE["timestamp"]) < max_cache_age
    ):
        return _SCAN_CACHE["data"]

    stats = {
        name: {"file_count": 0, "total_bytes": 0, "oldest": None, "newest": None}
        for name in CATEGORY_LABELS
    }
    age_cutoffs = {days: now - days * 86400 for days in AGE_BUCKETS}
    reclaimable = {days: {"file_count": 0, "bytes": 0} for days in AGE_BUCKETS}
    # Where the UI should say each category lives. Sidecars are written next to
    # the audio they describe, so they share that directory.
    directories = {
        VIDEOS: str(settings.output_videos),
        AUDIO: str(settings.output_audio),
        UPLOADS: str(settings.uploads_dir),
        SIDECARS: str(settings.output_audio),
        NOTE_MIRRORS: str(settings.output_transcripts),
    }

    for path, category, root in _iter_files():
        try:
            stat = path.stat()
        except Exception:
            continue
        entry = stats[category]
        entry["file_count"] += 1
        entry["total_bytes"] += stat.st_size
        if entry["oldest"] is None or stat.st_mtime < entry["oldest"]:
            entry["oldest"] = stat.st_mtime
        if entry["newest"] is None or stat.st_mtime > entry["newest"]:
            entry["newest"] = stat.st_mtime

        if category in RECLAIMABLE and not _is_protected(path):
            for days, cutoff in age_cutoffs.items():
                if stat.st_mtime < cutoff:
                    reclaimable[days]["file_count"] += 1
                    reclaimable[days]["bytes"] += stat.st_size

    categories = []
    for name, label in CATEGORY_LABELS.items():
        entry = stats[name]
        directory = directories.get(name) or ""
        categories.append({
            "name": name,
            "label": label,
            "directory": directory,
            "windows_directory": to_windows_path(directory) if directory else "",
            "file_count": entry["file_count"],
            "total_bytes": entry["total_bytes"],
            "total_mb": round(entry["total_bytes"] / (1024 ** 2), 1),
            "oldest_modified": (
                datetime.fromtimestamp(entry["oldest"]).isoformat() if entry["oldest"] else None
            ),
            "newest_modified": (
                datetime.fromtimestamp(entry["newest"]).isoformat() if entry["newest"] else None
            ),
            "reclaimable": name in RECLAIMABLE,
        })

    total_bytes = sum(c["total_bytes"] for c in categories)
    try:
        drive_stat = shutil.disk_usage("/mnt/d")
        drive = {
            "path": "/mnt/d",
            "total_gb": round(drive_stat.total / (1024 ** 3), 2),
            "used_gb": round(drive_stat.used / (1024 ** 3), 2),
            "free_gb": round(drive_stat.free / (1024 ** 3), 2),
        }
    except Exception as e:
        logger.warning(f"[storage] Could not read drive usage: {e}")
        drive = {"path": "/mnt/d", "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0}

    result = {
        "drive": drive,
        "categories": categories,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 ** 2), 1),
        "reclaimable_by_age": {
            str(days): {
                "days": days,
                "file_count": reclaimable[days]["file_count"],
                "bytes": reclaimable[days]["bytes"],
                "mb": round(reclaimable[days]["bytes"] / (1024 ** 2), 1),
            }
            for days in AGE_BUCKETS
        },
        "scanned_at": datetime.now().isoformat(),
    }

    _SCAN_CACHE.update({"timestamp": now, "key": key, "data": result})
    return result


def plan_reclaim(older_than_days: int, categories: List[str]) -> List[Dict[str, Any]]:
    """
    Files that an actual reclaim would delete, newest exclusions applied.

    Candidates must sit inside the category's own directory, must not be a note
    or a sidecar, and must not belong to a job that is still queued or running.
    """
    selected = [c for c in categories if c in RECLAIMABLE]
    if not selected:
        return []

    cutoff = time.time() - older_than_days * 86400
    protected_paths = _active_job_paths()
    root_for = {default: root for root, default in _scan_roots()}
    candidates = []

    for path, category, root in _iter_files():
        if category not in selected:
            continue
        if _is_protected(path):
            continue
        expected_root = root_for.get(category)
        if not expected_root or not is_path_allowed(path, [expected_root]):
            continue
        if str(path) in protected_paths:
            logger.info(f"[storage] Skipping {path.name}: referenced by an active job")
            continue
        try:
            stat = path.stat()
        except Exception:
            continue
        if stat.st_mtime >= cutoff:
            continue
        candidates.append({
            "path": str(path),
            "windows_path": to_windows_path(path),
            "category": category,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 ** 2), 2),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })

    candidates.sort(key=lambda c: c["size_bytes"], reverse=True)
    return candidates


def execute_reclaim(older_than_days: int, categories: List[str]) -> Dict[str, Any]:
    """Delete the planned candidates, one failure never aborting the rest."""
    candidates = plan_reclaim(older_than_days, categories)
    removed = []
    errors = []
    freed_bytes = 0

    for candidate in candidates:
        path = Path(candidate["path"])
        try:
            if path.exists():
                path.unlink()
                removed.append(candidate)
                freed_bytes += candidate["size_bytes"]
        except Exception as e:
            errors.append(f"{path.name}: {e}")
            logger.warning(f"[storage] Could not delete {path}: {e}")

    # Force the next scan to re-read the directories we just changed.
    _SCAN_CACHE.update({"timestamp": 0.0, "key": None, "data": None})

    if removed:
        logger.info(
            f"[storage] Reclaimed {len(removed)} file(s), "
            f"{freed_bytes / (1024 ** 2):.1f} MB (older than {older_than_days}d, {', '.join(categories)})"
        )

    return {
        "removed": removed,
        "file_count": len(removed),
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 ** 2), 2),
        "errors": errors,
    }


def invalidate_cache():
    """Drop the cached scan (used after a pipeline writes new output)."""
    _SCAN_CACHE.update({"timestamp": 0.0, "key": None, "data": None})
