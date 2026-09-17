import json
import os
import re
import urllib.parse
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from datetime import datetime

from app.models.schemas import IngestRequest, JobResponse, JobStatus
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.services import stt_service
from app.services.obsidian_service import ObsidianService, summarize_with_ollama
from app.config import normalize_path

router = APIRouter(prefix="/api/v1", tags=["Full Ingestion Pipeline"])

def run_ingest_pipeline(job_id: str, req: IngestRequest):
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        # Step 1: Download or resolve local file
        is_url = req.source.startswith("http://") or req.source.startswith("https://")
        if is_url:
            job_manager.update_job(job_id, progress_percent=15.0, speed="Downloading...")
            media_info = YtDlpService.download(req.source, audio_only=False)
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

        # Step 2: Optional clipping
        if req.clip_start and req.clip_end:
            job_manager.update_job(job_id, progress_percent=35.0, speed="Clipping...")
            current_media = FFmpegService.clip_media(
                source_path=current_media,
                start_time=req.clip_start,
                end_time=req.clip_end
            )
            media_info["file_path"] = str(current_media)

        # Step 3: Extract Audio & Transcribe
        job_manager.update_job(job_id, progress_percent=50.0, speed="Extracting Audio...")
        if current_media.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov", ".avi"]:
            audio_path = FFmpegService.extract_audio(current_media)
        else:
            audio_path = current_media

        engine = stt_service.resolve_engine(req.engine, None)
        job_manager.update_job(job_id, progress_percent=70.0, speed=f"Transcribing with {engine}...")
        trans_res = stt_service.transcribe(audio_path, engine=engine, model_size=req.model_size)

        # Full result sidecar for GUI audio player timestamp seeking and sync
        out_json = audio_path.with_suffix(".transcript.json")
        try:
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump({**trans_res, "media": media_info, "source": req.source, "audio": str(audio_path)}, f, ensure_ascii=False, indent=1)
        except Exception:
            out_json = None

        # Step 4: Executive AI Summary via Ollama
        summary = "*(Summary disabled)*"
        if req.summarize:
            job_manager.update_job(job_id, progress_percent=85.0, speed="Generating Ollama Summary...")
            summary = summarize_with_ollama(media_info["title"], trans_res["full_text"], req.llm_model)

        # Step 5: Save Note directly to Obsidian Vault Transcripts
        job_manager.update_job(job_id, progress_percent=95.0, speed="Writing Obsidian Vault Note...")
        clean_sub = re.sub(r"\.\.+[/\\]?", "", req.vault_subfolder or "Transcripts").strip("/\\") or "Transcripts"
        vault_note = ObsidianService.create_transcript_note(
            media_info=media_info,
            transcription=trans_res,
            summary=summary,
            vault_subfolder=clean_sub,
            custom_tags=req.tags
        )

        # Step 6: Trigger Semantic Vector Re-Index (Non-blocking)
        try:
            import subprocess
            subprocess.Popen(["find-concept", "--reindex"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        encoded_note = urllib.parse.quote(f"{clean_sub}/{vault_note.name}")
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
            obsidian_link=f"[[{clean_sub}/{vault_note.stem}]]",
            obsidian_uri=f"obsidian://open?vault=Vault&file={encoded_note}",
            output_files=out_files
        )
    except Exception as e:
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))

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
