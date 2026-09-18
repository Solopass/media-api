import json
import os
import re
import shutil
import logging
import traceback
import urllib.parse
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from datetime import datetime

from app.models.schemas import (
    IngestRequest, JobResponse, JobStatus, ResummarizeRequest, ResummarizeResponse,
    PlaylistExpandRequest, PlaylistExpandResponse, PlaylistItem
)
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.services import stt_service
from app.services.obsidian_service import ObsidianService, summarize_with_ollama
from app.config import settings, normalize_path

logger = logging.getLogger("media_api")

router = APIRouter(prefix="/api/v1", tags=["Full Ingestion Pipeline"])

def run_ingest_pipeline(job_id: str, req: IngestRequest):
    if job_manager.is_cancelled(job_id):
        return

    job_manager.pipeline_started()
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    stage = "Initialization"
    media_info = {"title": "Unknown Media", "url": req.source}
    partial_files = {}

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
        else:
            current_media = normalize_path(req.source)
            if not current_media.exists():
                raise FileNotFoundError(f"Media not found: {current_media}")
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

        # Check cancellation
        if job_manager.is_cancelled(job_id):
            return

        # Step 3: Extract Audio & Transcribe
        stage = "Audio Extraction"
        job_manager.update_job(job_id, progress_percent=50.0, speed="Extracting 16kHz audio for STT...")
        video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".wmv"}
        non_native_audio = {".opus", ".wma"}
        if current_media.suffix.lower() in video_exts or current_media.suffix.lower() in non_native_audio:
            audio_path = FFmpegService.extract_audio(current_media)
        else:
            audio_path = current_media
        partial_files["audio"] = str(audio_path)

        if job_manager.is_cancelled(job_id):
            return

        stage = "Speech-to-Text Transcription"
        engine = stt_service.resolve_engine(req.engine, None)
        job_manager.update_job(job_id, progress_percent=65.0, speed=f"Transcribing audio with {engine}...")
        trans_res = stt_service.transcribe(audio_path, engine=engine, model_size=req.model_size)

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
            summary_focus=req.summary_focus
        )

        # Step 6: Trigger Semantic Vector Re-Index (Non-blocking)
        try:
            import subprocess
            subprocess.Popen(["find-concept", "--reindex"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        encoded_note = urllib.parse.quote(f"Agent/{clean_sub}/{vault_note.name}")
        out_files = {
            "media": str(current_media),
            "audio": str(audio_path),
            "vault_note": str(vault_note)
        }
        if out_json and Path(out_json).exists():
            out_files["transcript_json"] = str(out_json)

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            speed="Completed successfully",
            obsidian_link=f"[[Agent/{clean_sub}/{vault_note.stem}]]",
            obsidian_uri=f"obsidian://open?vault={settings.vault_name}&file={encoded_note}",
            output_files=out_files
        )
        logger.info(f"[ingest] Ingestion pipeline succeeded for job {job_id}: {vault_note.name}")

    except Exception as e:
        if not job_manager.is_cancelled(job_id):
            tb_str = traceback.format_exc()
            logger.exception(f"[ingest] Pipeline glitched at stage '{stage}' for job {job_id}: {e}")
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
        job_manager.pipeline_ended()

@router.post("/ingest", response_model=JobResponse)
def ingest_media(req: IngestRequest, background_tasks: BackgroundTasks):
    try:
        stt_service.resolve_engine(req.engine, None)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    job_id = job_manager.create_job("ingest", req.source)
    background_tasks.add_task(run_ingest_pipeline, job_id, req)
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
            items=[PlaylistItem(**item) for item in data.get("items", [])]
        )
    except Exception as e:
        logger.warning(f"[batch] Failed to expand playlist for {req.url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to expand playlist: {str(e)}")

