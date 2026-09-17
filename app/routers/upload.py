import mimetypes
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import settings, normalize_path, to_windows_path

router = APIRouter(prefix="/api/v1", tags=["Media Upload & Streaming"])

ALLOWED_MEDIA_ROOTS = [
    Path("/mnt/d/Output"),
    Path("/mnt/d/AI/Cache"),
    Path("/mnt/d/AI/Vault")
]

ALLOWED_NOTE_ROOTS = [
    Path("/mnt/d/AI/Vault"),
    Path("/mnt/d/Output/Transcripts")
]

ALLOWED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".mp4", ".webm", ".mkv", ".mov", ".avi",
    ".json", ".txt", ".md"
}

def _sanitize_filename(name: str) -> str:
    base = os.path.basename(name)
    clean = re.sub(r"[^\w\s\.-]", "_", base).strip()
    return clean or "uploaded_media"

def _is_path_allowed(target: Path, roots: list[Path]) -> bool:
    try:
        resolved_target = target.resolve()
        for root in roots:
            resolved_root = root.resolve()
            if resolved_target == resolved_root or resolved_target.is_relative_to(resolved_root):
                return True
    except Exception:
        return False
    return False

@router.post("/upload")
async def upload_media_file(file: UploadFile = File(...)):
    """Accept an uploaded media file and stream it directly to D:\\AI\\Cache\\uploads\\."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename missing")

    clean_name = _sanitize_filename(file.filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_filename = f"{timestamp}_{clean_name}"
    dest_path = settings.uploads_dir / dest_filename

    total_bytes = 0
    buffer_size = 1024 * 1024  # 1 MB chunk

    try:
        with open(dest_path, "wb") as f:
            while chunk := await file.read(buffer_size):
                f.write(chunk)
                total_bytes += len(chunk)
    except Exception as e:
        if dest_path.exists():
            dest_path.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}")

    return {
        "status": "ok",
        "filename": clean_name,
        "dest_filename": dest_filename,
        "file_path": str(dest_path),
        "windows_path": to_windows_path(dest_path),
        "size_bytes": total_bytes
    }

@router.get("/media/stream")
def stream_media(path: str = Query(..., description="File path to stream")):
    """Stream audio/video file with HTTP Range support for HTML5 players."""
    target_path = normalize_path(path)

    if not _is_path_allowed(target_path, ALLOWED_MEDIA_ROOTS):
        raise HTTPException(status_code=403, detail="Access denied: path outside permitted media directories")

    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    if target_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {target_path.suffix}")

    media_type, _ = mimetypes.guess_type(str(target_path))
    if not media_type:
        if target_path.suffix.lower() in [".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"]:
            media_type = f"audio/{target_path.suffix.lstrip('.')}"
        else:
            media_type = "application/octet-stream"

    return FileResponse(
        path=str(target_path),
        media_type=media_type,
        filename=target_path.name
    )

@router.get("/vault/note")
def get_vault_note(path: str = Query(..., description="Path to markdown note in vault")):
    """Retrieve Obsidian markdown note content and deep-link details."""
    target_path = normalize_path(path)

    if not _is_path_allowed(target_path, ALLOWED_NOTE_ROOTS):
        raise HTTPException(status_code=403, detail="Access denied: path outside Obsidian vault")

    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Vault note not found")

    if target_path.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="Requested file is not a Markdown note")

    try:
        content = target_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read note: {e}")

    try:
        rel_to_vault = target_path.resolve().relative_to(Path("/mnt/d/AI/Vault").resolve())
    except Exception:
        try:
            rel_to_vault = target_path.resolve().relative_to(Path("/mnt/d/Output/Transcripts").resolve())
            rel_to_vault = Path("Transcripts") / rel_to_vault
        except Exception:
            rel_to_vault = Path(f"Transcripts/{target_path.name}")

    rel_posix = rel_to_vault.as_posix()
    encoded_file = urllib.parse.quote(rel_posix)
    obsidian_uri = f"obsidian://open?vault=Vault&file={encoded_file}"
    parent_posix = rel_to_vault.parent.as_posix()
    obsidian_link = f"[[{parent_posix}/{target_path.stem}]]" if parent_posix != "." else f"[[{target_path.stem}]]"

    return {
        "filename": target_path.name,
        "stem": target_path.stem,
        "file_path": str(target_path),
        "windows_path": to_windows_path(target_path),
        "obsidian_uri": obsidian_uri,
        "obsidian_link": obsidian_link,
        "content": content
    }

def _extract_frontmatter(content: str) -> dict:
    """Safely parse YAML frontmatter from markdown note content."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                data = yaml.safe_load(parts[1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}

@router.get("/vault/recent")
def get_recent_vault_notes(limit: int = Query(15, ge=1, le=100, description="Max recent notes to return")):
    """Scan D:\\AI\\Vault\\Transcripts recursively for recent markdown transcript notes."""
    search_dir = settings.vault_transcripts
    if not search_dir.exists():
        return {"notes": [], "total": 0}

    candidates = []
    try:
        for p in search_dir.rglob("*.md"):
            if p.is_file() and p.name != "Transcripts_Dashboard.md" and not p.name.startswith("."):
                candidates.append(p)
    except Exception:
        pass

    # Sort candidates by mtime descending
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    selected = candidates[:limit]

    results = []
    for p in selected:
        try:
            stat = p.stat()
            header_chunk = ""
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                header_chunk = f.read(4096)
            fm = _extract_frontmatter(header_chunk)
            title = fm.get("title") or p.stem
            date_val = str(fm.get("date") or datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))
            duration_val = str(fm.get("duration") or "")
            tags_val = fm.get("tags") or []
            if isinstance(tags_val, str):
                tags_val = [t.strip() for t in tags_val.split(",") if t.strip()]

            try:
                rel_to_vault = p.resolve().relative_to(Path("/mnt/d/AI/Vault").resolve())
            except Exception:
                rel_to_vault = Path(f"Transcripts/{p.name}")

            rel_posix = rel_to_vault.as_posix()
            encoded_file = urllib.parse.quote(rel_posix)
            obsidian_uri = f"obsidian://open?vault=Vault&file={encoded_file}"
            parent_posix = rel_to_vault.parent.as_posix()
            obsidian_link = f"[[{parent_posix}/{p.stem}]]" if parent_posix != "." else f"[[{p.stem}]]"

            results.append({
                "filename": p.name,
                "stem": p.stem,
                "file_path": str(p),
                "windows_path": to_windows_path(p),
                "title": title,
                "date": date_val,
                "duration": duration_val,
                "tags": tags_val,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "obsidian_uri": obsidian_uri,
                "obsidian_link": obsidian_link
            })
        except Exception:
            continue

    return {"notes": results, "total": len(candidates)}

