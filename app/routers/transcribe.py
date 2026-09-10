import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from datetime import datetime

from app.models.schemas import TranscribeRequest, JobResponse, JobStatus
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.services.whisper_service import WhisperService
from app.services.obsidian_service import ObsidianService
from app.config import normalize_path

router = APIRouter(prefix="/api/v1", tags=["Speech-To-Text Transcription"])

def run_transcribe_task(job_id: str, req: TranscribeRequest):
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        # Handle online media URL
        if req.source.startswith("http://") or req.source.startswith("https://"):
            job_manager.update_job(job_id, progress_percent=10.0, speed="Downloading...")
            info = YtDlpService.download(req.source, audio_only=True, target_format="mp3")
            source_path = Path(info["file_path"])
        else:
            source_path = normalize_path(req.source)

        if not source_path.exists():
            raise FileNotFoundError(f"Source media not found: {source_path}")

        # Extract audio if it is a video container
        if source_path.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov", ".avi"]:
            job_manager.update_job(job_id, progress_percent=30.0, speed="Extracting Audio...")
            audio_path = FFmpegService.extract_audio(source_path)
        else:
            audio_path = source_path

        job_manager.update_job(job_id, progress_percent=50.0, speed="Transcribing with Whisper...")
        trans_res = WhisperService.transcribe(
            audio_path=audio_path,
            model_size=req.model_size,
            language=req.language
        )

        out_txt = audio_path.with_suffix(".txt")
        with open(out_txt, "w", encoding="utf-8") as f:
            f.write(trans_res["full_text"])

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            output_files={
                "audio": str(audio_path),
                "text": str(out_txt)
            }
        )
    except Exception as e:
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))

@router.post("/transcribe", response_model=JobResponse)
def transcribe_media(req: TranscribeRequest, background_tasks: BackgroundTasks):
    job_id = job_manager.create_job("transcribe", req.source)
    background_tasks.add_task(run_transcribe_task, job_id, req)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message=f"Transcription queued (model: {req.model_size})",
        created_at=datetime.now().isoformat()
    )
