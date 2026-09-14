import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks
from datetime import datetime

from app.models.schemas import IngestRequest, JobResponse, JobStatus
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.services.whisper_service import WhisperService
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

        job_manager.update_job(job_id, progress_percent=70.0, speed="Transcribing with Whisper...")
        trans_res = WhisperService.transcribe(audio_path, model_size=req.model_size)

        # Step 4: Executive AI Summary via Ollama
        summary = "*(Summary disabled)*"
        if req.summarize:
            job_manager.update_job(job_id, progress_percent=85.0, speed="Generating Ollama Summary...")
            summary = summarize_with_ollama(media_info["title"], trans_res["full_text"], req.llm_model)

        # Step 5: Save Note directly to Obsidian Vault Transcripts
        job_manager.update_job(job_id, progress_percent=95.0, speed="Writing Obsidian Vault Note...")
        vault_note = ObsidianService.create_transcript_note(
            media_info=media_info,
            transcription=trans_res,
            summary=summary,
            model_size=req.model_size
        )

        # Step 6: Trigger Semantic Vector Re-Index (Non-blocking)
        try:
            import subprocess
            subprocess.run(["find-concept", "--reindex"], capture_output=True, timeout=15)
        except Exception:
            pass

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            obsidian_link=f"[[Transcripts/{vault_note.stem}]]",
            output_files={
                "media": str(current_media),
                "audio": str(audio_path),
                "vault_note": str(vault_note)
            }
        )
    except Exception as e:
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))

@router.post("/ingest", response_model=JobResponse)
def ingest_media(req: IngestRequest, background_tasks: BackgroundTasks):
    job_id = job_manager.create_job("ingest", req.source)
    background_tasks.add_task(run_ingest_pipeline, job_id, req)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message="One-shot media ingestion pipeline queued",
        created_at=datetime.now().isoformat()
    )
