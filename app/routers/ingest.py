import json
import os
import time
import re
import shutil
import logging
import subprocess
import threading
import traceback
import urllib.parse
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from datetime import datetime

from app.models.schemas import (
    IngestRequest, JobResponse, JobStatus, ResummarizeRequest, ResummarizeResponse,
    PlaylistExpandRequest, PlaylistExpandResponse, PlaylistItem,
    ChaptersRequest, ChaptersResponse
)
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService, is_collection_url
from app.services.ffmpeg_service import FFmpegService
from app.services import stt_service
from app.services.obsidian_service import ObsidianService, summarize_with_ollama
from app.services import chapters_service
from app.config import settings, normalize_path
from app.paths import is_inside, is_path_allowed, note_roots

logger = logging.getLogger("media_api")

router = APIRouter(prefix="/api/v1", tags=["Full Ingestion Pipeline"])

# Safety net for cleanup: even a file the pipeline believes it produced must
# live somewhere this API writes into before it can be deleted.
MANAGED_MEDIA_DIRS = (settings.output_videos, settings.output_audio, settings.uploads_dir)


def _is_managed_media(path: Path) -> bool:
    """True when path sits inside a directory this API writes into."""
    return is_path_allowed(path, list(MANAGED_MEDIA_DIRS))


def _discard_source_video(candidates: list[Path], keep: Optional[Path]) -> tuple[list[str], float]:
    """
    Delete the (large) source video once the transcript exists.

    The extracted audio and transcript sidecar are kept: the UI player streams
    them back. Returns the paths actually removed and the MB reclaimed.
    """
    removed = []
    freed_mb = 0.0
    seen = set()
    for path in candidates:
        if not path or path == keep or str(path) in seen:
            continue
        seen.add(str(path))
        if not _is_managed_media(path):
            logger.info(f"[ingest] Keeping {path} (outside managed output dirs)")
            continue
        try:
            if path.exists():
                size_mb = path.stat().st_size / (1024 * 1024)
                path.unlink()
                removed.append(str(path))
                freed_mb += size_mb
                logger.info(f"[ingest] Removed source video {path.name} ({size_mb:.1f} MB)")
        except Exception as e:
            logger.warning(f"[ingest] Could not remove source video {path}: {e}")
    return removed, round(freed_mb, 1)


def _format_clock(seconds: float) -> str:
    """Seconds as H:MM:SS or M:SS, for showing position inside a long recording."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_eta(seconds: float) -> str:
    """Coarse ETA: precision here would be false confidence."""
    if seconds <= 0:
        return ""
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(round(minutes))}m"
    return f"{minutes / 60:.1f}h"


def _stage_progress(job_id: str, started_at: float, band_start: float, band_end: float, label: str):
    """
    Build a progress callback that maps a stage's 0..1 fraction onto its slice of
    the overall pipeline bar, with a position and an ETA extrapolated from the
    rate so far.
    """
    def _report(fraction: float, position_seconds: float, estimated: bool = False):
        if job_manager.is_cancelled(job_id):
            return
        pct = band_start + (band_end - band_start) * max(0.0, min(1.0, fraction))
        elapsed = time.time() - started_at
        # "~" and "est." keep an extrapolated position from reading as measured.
        prefix = "~" if estimated else ""
        detail = f"{label} {prefix}{_format_clock(position_seconds)}"
        if fraction > 0.01 and elapsed > 5:
            eta = _format_eta((elapsed / fraction) - elapsed)
            if eta:
                detail += f" ({int(fraction * 100)}%{' est.' if estimated else ''}, ETA {eta})"
        else:
            detail += f" ({int(fraction * 100)}%)"
        job_manager.update_job(job_id, progress_percent=round(pct, 1), speed=detail)

    return _report


def _completion_message(removed_media, freed_mb, chapters) -> str:
    """Status line for the UI: what was written, and what was reclaimed."""
    parts = ["Completed successfully"]
    if chapters:
        parts.append(f"{len(chapters)} chapters")
    if removed_media:
        parts.append(f"source video deleted, {freed_mb:.0f} MB freed")
    return parts[0] if len(parts) == 1 else f"{parts[0]} ({' · '.join(parts[1:])})"


def _trigger_reindex():
    """Fire the vector re-index and reap the child so it cannot linger as a zombie."""
    def _run():
        try:
            subprocess.run(
                ["find-concept", "--reindex"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300
            )
        except Exception as e:
            logger.debug(f"[ingest] Vector re-index skipped: {e}")

    threading.Thread(target=_run, name="reindex", daemon=True).start()

def run_ingest_pipeline(job_id: str, req: IngestRequest):
    if job_manager.is_cancelled(job_id):
        return

    job_manager.pipeline_started()
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    stage = "Initialization"
    media_info = {"title": "Unknown Media", "url": req.source}
    partial_files = {}
    # Media this job created, and therefore may clean up. A file the user keeps
    # elsewhere - including one already sitting in D:\Output\Videos - is never
    # in here, so re-ingesting it by path leaves it alone.
    produced_media: list[Path] = []
    audio_path: Optional[Path] = None

    def _discard_unfinished_media():
        """Drop this job's download when the run ends without a note."""
        if req.keep_video or not produced_media:
            return
        removed, freed_mb = _discard_source_video(produced_media, keep=audio_path)
        if removed:
            for key in ("video", "clipped"):
                if partial_files.get(key) in removed:
                    partial_files.pop(key, None)
            logger.info(f"[ingest] Aborted job {job_id}: reclaimed {freed_mb:.0f} MB of unused media")

    try:
        # Step 0: Pre-flight storage check (minimum 3 GB free on D:)
        d_free = shutil.disk_usage("/mnt/d").free
        if d_free < 3 * 1024 * 1024 * 1024:
            raise RuntimeError(f"Insufficient D: drive storage ({d_free // (1024**2)} MB free, requires at least 3000 MB)")

        if job_manager.is_cancelled(job_id):
            return

        # Step 1: Download or resolve local file
        stage = "Download / Resolve Media"
        is_url = req.source.startswith("http://") or req.source.startswith("https://")
        if is_url:
            job_manager.update_job(job_id, progress_percent=15.0, speed="Starting download...")

            def _dl_progress(d):
                if job_manager.is_cancelled(job_id):
                    raise RuntimeError("Download cancelled by user")
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                    downloaded = d.get("downloaded_bytes", 0)
                    pct = 15.0 + min(34.0, (downloaded / total) * 34.0)
                    speed = d.get("speed") or 0
                    speed_mb = speed / (1024 * 1024)
                    eta = d.get("eta") or 0
                    job_manager.update_job(
                        job_id,
                        progress_percent=round(pct, 1),
                        speed=f"Downloading ({round(downloaded / (1024*1024), 1)}/{round(total / (1024*1024), 1)} MB - {speed_mb:.1f} MB/s, ETA {eta}s)"
                    )

            media_info = YtDlpService.download(req.source, audio_only=False, progress_hook=_dl_progress)
            current_media = Path(media_info["file_path"])
            produced_media.append(current_media)
        else:
            current_media = normalize_path(req.source)
            if not current_media.exists():
                raise FileNotFoundError(f"Media not found: {current_media}")
            # An upload-cache copy belongs to this job; anything else is the user's.
            if is_inside(current_media, settings.uploads_dir):
                produced_media.append(current_media)
            media_info = {
                "title": current_media.stem,
                "file_path": str(current_media),
                "uploader": "Local Ingest",
                "url": str(current_media)
            }

        partial_files["video"] = str(current_media)

        # Check cancellation
        if job_manager.is_cancelled(job_id):
            return

        # Step 2: Optional clipping
        if req.clip_start and req.clip_end:
            stage = "Clipping Media"
            job_manager.update_job(job_id, progress_percent=40.0, speed="Clipping media segment...")
            current_media = FFmpegService.clip_media(
                source_path=current_media,
                start_time=req.clip_start,
                end_time=req.clip_end
            )
            media_info["file_path"] = str(current_media)
            partial_files["clipped"] = str(current_media)
            produced_media.append(current_media)

        # Check cancellation
        if job_manager.is_cancelled(job_id):
            return

        # Step 3: Extract Audio & Transcribe
        stage = "Audio Extraction"
        job_manager.update_job(job_id, progress_percent=50.0, speed="Extracting audio...")
        video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".wmv"}
        non_native_audio = {".opus", ".wma"}
        if current_media.suffix.lower() in video_exts or current_media.suffix.lower() in non_native_audio:
            audio_path = FFmpegService.extract_audio(
                current_media,
                progress_callback=_stage_progress(job_id, time.time(), 50.0, 62.0, "Extracting audio"),
            )
        else:
            audio_path = current_media
        partial_files["audio"] = str(audio_path)

        if job_manager.is_cancelled(job_id):
            return

        stage = "Speech-to-Text Transcription"
        engine = stt_service.resolve_engine(req.engine, req.language)
        job_manager.update_job(job_id, progress_percent=65.0, speed=f"Transcribing audio with {engine}...")
        trans_res = stt_service.transcribe(
            audio_path, engine=engine, model_size=req.model_size, language=req.language,
            progress_callback=_stage_progress(
                job_id, time.time(), 65.0, 84.0, f"Transcribing ({engine})"
            ),
            should_cancel=lambda: job_manager.is_cancelled(job_id),
            status_callback=lambda text: job_manager.update_job(job_id, speed=text),
        )

        if job_manager.is_cancelled(job_id):
            return

        # Full result sidecar for GUI audio player timestamp seeking and sync
        out_json = audio_path.with_suffix(".transcript.json")
        try:
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump({**trans_res, "media": media_info, "source": req.source, "audio": str(audio_path)}, f, ensure_ascii=False, indent=1)
            partial_files["transcript_json"] = str(out_json)
        except Exception:
            out_json = None

        # Step 4: Executive AI Summary via Ollama
        stage = "Ollama AI Summary"
        summary = "*(Summary disabled)*"
        if req.summarize:
            if job_manager.is_cancelled(job_id):
                return
            job_manager.update_job(job_id, progress_percent=85.0, speed=f"Generating {req.summary_preset} summary via {req.llm_model}...")
            summary = summarize_with_ollama(
                title=media_info["title"],
                text=trans_res["full_text"],
                model_name=req.llm_model,
                preset=req.summary_preset,
                focus=req.summary_focus
            )

        if job_manager.is_cancelled(job_id):
            return

        # Step 4b: Optional chapter markers (a second, separate LLM pass)
        chapters = []
        if req.generate_chapters:
            stage = "Chapter Generation"
            job_manager.update_job(job_id, progress_percent=90.0, speed="Generating chapter markers...")
            chapters = chapters_service.generate_chapters(
                title=media_info["title"],
                segments=trans_res.get("segments", []),
                duration=float(trans_res.get("duration") or 0.0),
                model_name=req.llm_model
            )

        if job_manager.is_cancelled(job_id):
            return

        # Step 5: Save Note directly to Obsidian Vault Transcripts
        stage = "Writing Obsidian Vault Note"
        job_manager.update_job(job_id, progress_percent=95.0, speed="Writing Obsidian Vault Note...")
        clean_sub = re.sub(r"\.\.+[/\\]?", "", req.vault_subfolder or "Transcripts").strip("/\\") or "Transcripts"
        vault_note = ObsidianService.create_transcript_note(
            media_info=media_info,
            transcription=trans_res,
            summary=summary,
            vault_subfolder=clean_sub,
            custom_tags=req.tags,
            summary_preset=req.summary_preset,
            summary_focus=req.summary_focus,
            chapters=chapters
        )

        # Step 6: Discard the source video unless the user asked to keep it
        removed_media, freed_mb = [], 0.0
        if not req.keep_video:
            removed_media, freed_mb = _discard_source_video(produced_media, keep=audio_path)

        # Step 7: Trigger Semantic Vector Re-Index (Non-blocking)
        _trigger_reindex()

        encoded_note = urllib.parse.quote(f"Agent/{clean_sub}/{vault_note.name}")
        out_files = {
            "audio": str(audio_path),
            "vault_note": str(vault_note)
        }
        if current_media.exists():
            out_files["media"] = str(current_media)
        if out_json and Path(out_json).exists():
            # Re-write the sidecar now that chapters exist, so the UI can render
            # them without parsing the markdown note.
            if chapters:
                try:
                    sidecar = json.loads(Path(out_json).read_text(encoding="utf-8"))
                    sidecar["chapters"] = chapters
                    Path(out_json).write_text(
                        json.dumps(sidecar, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                except Exception as e:
                    logger.warning(f"[ingest] Could not add chapters to sidecar: {e}")
            out_files["transcript_json"] = str(out_json)

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            speed=_completion_message(removed_media, freed_mb, chapters),
            obsidian_link=f"[[Agent/{clean_sub}/{vault_note.stem}]]",
            obsidian_uri=f"obsidian://open?vault={settings.vault_name}&file={encoded_note}",
            output_files=out_files
        )
        logger.info(f"[ingest] Ingestion pipeline succeeded for job {job_id}: {vault_note.name}")

    except Exception as e:
        if not job_manager.is_cancelled(job_id):
            tb_str = traceback.format_exc()
            logger.exception(f"[ingest] Pipeline glitched at stage '{stage}' for job {job_id}: {e}")
            # Reclaim the download first, so the error note only lists files
            # that are actually still on disk.
            _discard_unfinished_media()
            clean_sub = re.sub(r"\.\.+[/\\]?", "", req.vault_subfolder or "Transcripts").strip("/\\") or "Transcripts"
            try:
                error_note = ObsidianService.create_error_report_note(
                    media_info=media_info,
                    failed_stage=stage,
                    error_message=str(e),
                    traceback_str=tb_str,
                    partial_files=partial_files,
                    vault_subfolder=clean_sub
                )
                encoded_note = urllib.parse.quote(f"Agent/{clean_sub}/{error_note.name}")
                obs_link = f"[[Agent/{clean_sub}/{error_note.stem}]]"
                obs_uri = f"obsidian://open?vault={settings.vault_name}&file={encoded_note}"
            except Exception as note_err:
                logger.error(f"[ingest] Failed to generate Obsidian error report note: {note_err}")
                obs_link = None
                obs_uri = None

            job_manager.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=f"Failed at {stage}: {e}",
                obsidian_link=obs_link,
                obsidian_uri=obs_uri,
                output_files=partial_files
            )
    finally:
        if job_manager.is_cancelled(job_id):
            _discard_unfinished_media()
        job_manager.pipeline_ended()

@router.post("/ingest", response_model=JobResponse)
def ingest_media(req: IngestRequest):
    # A playlist or channel URL expands to many videos; handing it to the
    # single-item pipeline makes yt-dlp pick an arbitrary entry, which fails
    # confusingly when that entry happens to be unavailable.
    if is_collection_url(req.source):
        raise HTTPException(
            status_code=400,
            detail=(
                "That is a playlist or channel URL, which contains many videos. "
                "Use Batch mode (or POST /api/v1/batch/expand-playlist) to pick and queue them."
            )
        )
    try:
        stt_service.resolve_engine(req.engine, req.language)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    job_id = job_manager.create_job("ingest", req.source)
    # Queued on the single pipeline worker: one GPU job at a time, in order.
    job_manager.submit_pipeline(run_ingest_pipeline, job_id, req)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message="One-shot media ingestion pipeline queued",
        created_at=datetime.now().isoformat()
    )

@router.post("/vault/resummarize", response_model=ResummarizeResponse)
def resummarize_note(req: ResummarizeRequest):
    """Regenerate an AI summary for an existing transcript note without re-transcribing."""
    raw_path = req.note_path.strip()
    target_file = normalize_path(raw_path)

    # If relative or filename only, search in vault_transcripts and output_transcripts
    if not target_file.exists():
        candidate = settings.vault_transcripts / raw_path
        if candidate.exists():
            target_file = candidate
        else:
            candidate2 = settings.output_transcripts / raw_path
            if candidate2.exists():
                target_file = candidate2
            else:
                raise HTTPException(status_code=404, detail=f"Note file not found: {raw_path}")

    # This endpoint rewrites the file in place, so it is held to the same roots
    # as /vault/note - and to Markdown, since a note without an abstract callout
    # gets the new summary prepended.
    if not is_path_allowed(target_file, note_roots()):
        raise HTTPException(status_code=403, detail="Access denied: path outside Obsidian vault")
    if target_file.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="Target is not a Markdown note")

    content = target_file.read_text(encoding="utf-8")

    # Extract title
    title_match = re.search(r"^title:\s*\"?(.*?)\"?$", content, re.MULTILINE)
    title = title_match.group(1) if title_match else target_file.stem

    # Extract text from corresponding transcript json or markdown table
    transcript_text = ""
    source_match = re.search(r"- \*\*Source Media\*\*:\s*`([^`]+)`", content)
    if source_match:
        media_p = Path(source_match.group(1))
        stem = media_p.stem
        for p in [settings.output_audio, settings.output_transcripts, settings.output_videos]:
            json_cand = p / f"{stem}.transcript.json"
            if json_cand.exists():
                try:
                    j_data = json.loads(json_cand.read_text(encoding="utf-8"))
                    transcript_text = j_data.get("full_text") or ""
                    if transcript_text:
                        break
                except Exception:
                    pass

    # Fallback: extract from markdown table
    if not transcript_text:
        table_lines = re.findall(r"\|\s*`\d+:\d+`\s*\|\s*(.*?)\s*\|", content)
        if table_lines:
            transcript_text = " ".join(table_lines)

    if not transcript_text:
        raise HTTPException(status_code=400, detail="Could not extract transcript text from note or associated transcript JSON")

    summary = summarize_with_ollama(
        title=title,
        text=transcript_text,
        model_name=req.llm_model,
        preset=req.summary_preset,
        focus=req.summary_focus
    )

    success = ObsidianService.update_note_summary(
        note_path=target_file,
        new_summary=summary,
        summary_preset=req.summary_preset,
        summary_focus=req.summary_focus
    )

    return ResummarizeResponse(
        success=success,
        note_path=str(target_file),
        summary=summary,
        model=req.llm_model,
        preset=req.summary_preset
    )

def _load_segments_for_note(content: str, target_file: Path):
    """
    Recover timestamped segments for an existing note.

    Prefers the .transcript.json sidecar; falls back to the note's own
    timestamped table, which gives coarser but still usable anchors.
    """
    source_match = re.search(r"- \*\*Source Media\*\*:\s*`([^`]+)`", content)
    if source_match:
        stem = Path(source_match.group(1)).stem
        for directory in (settings.output_audio, settings.output_transcripts, settings.output_videos):
            candidate = directory / f"{stem}.transcript.json"
            if candidate.exists():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    segments = data.get("segments") or []
                    if segments:
                        duration = float(data.get("duration") or 0.0)
                        if not duration:
                            duration = max(float(s.get("end") or s.get("start") or 0) for s in segments)
                        return segments, duration, candidate
                except Exception:
                    continue

    rows = re.findall(r"\|\s*`(\d+):(\d+)`\s*\|\s*(.*?)\s*\|", content)
    if rows:
        segments = [
            {"start": int(m) * 60 + int(sec), "end": int(m) * 60 + int(sec) + 1, "text": text}
            for m, sec, text in rows
        ]
        duration = segments[-1]["start"] if segments else 0.0
        return segments, float(duration), None

    return [], 0.0, None


@router.post("/vault/chapters", response_model=ChaptersResponse)
def generate_note_chapters(req: ChaptersRequest):
    """Generate chapter markers for an existing transcript note, in place."""
    raw_path = req.note_path.strip()
    target_file = normalize_path(raw_path)

    if not target_file.exists():
        for base in (settings.vault_transcripts, settings.output_transcripts):
            candidate = base / raw_path
            if candidate.exists():
                target_file = candidate
                break
        else:
            raise HTTPException(status_code=404, detail=f"Note file not found: {raw_path}")

    if not is_path_allowed(target_file, note_roots()):
        raise HTTPException(status_code=403, detail="Access denied: path outside Obsidian vault")
    if target_file.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="Target is not a Markdown note")

    content = target_file.read_text(encoding="utf-8")
    title_match = re.search(r"^title:\s*\"?(.*?)\"?$", content, re.MULTILINE)
    title = title_match.group(1) if title_match else target_file.stem

    segments, duration, sidecar_path = _load_segments_for_note(content, target_file)
    if not segments:
        raise HTTPException(
            status_code=400,
            detail="No timestamped transcript found for this note (sidecar missing and no transcript table)"
        )

    chapters = chapters_service.generate_chapters(
        title=title, segments=segments, duration=duration, model_name=req.llm_model
    )
    if not chapters:
        raise HTTPException(
            status_code=422,
            detail="The model did not return usable chapter markers for this transcript"
        )

    ObsidianService.update_note_chapters(target_file, chapters)

    if sidecar_path and sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            data["chapters"] = chapters
            sidecar_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[chapters] Could not update sidecar {sidecar_path}: {e}")

    return ChaptersResponse(
        success=True,
        note_path=str(target_file),
        chapters=chapters,
        youtube_block=chapters_service.to_youtube_block(chapters),
        model=req.llm_model
    )


@router.post("/batch/expand-playlist", response_model=PlaylistExpandResponse)
def expand_playlist(req: PlaylistExpandRequest):
    """
    Extract flat metadata and video list from a YouTube playlist, channel, or video URL.
    Used by the Batch Queue UI to preview and selectively ingest videos.
    """
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="A valid URL must be provided")

    try:
        data = YtDlpService.extract_playlist(req.url.strip(), max_items=req.max_items)
        return PlaylistExpandResponse(
            title=data.get("title", "Playlist"),
            uploader=data.get("uploader"),
            total_items=data.get("total_items", 0),
            skipped_unavailable=data.get("skipped_unavailable", 0),
            items=[PlaylistItem(**item) for item in data.get("items", [])]
        )
    except Exception as e:
        logger.warning(f"[batch] Failed to expand playlist for {req.url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to expand playlist: {str(e)}")

