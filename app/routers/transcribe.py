import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from datetime import datetime

from app.models.schemas import TranscribeRequest, JobResponse, JobStatus
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.services import stt_service
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

        engine = stt_service.resolve_engine(req.engine, req.language)
        job_manager.update_job(job_id, progress_percent=50.0, speed=f"Transcribing with {engine}...")
        trans_res = stt_service.transcribe(
            audio_path=audio_path,
            engine=engine,
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
    try:
        engine = stt_service.resolve_engine(req.engine, req.language)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    job_id = job_manager.create_job("transcribe", req.source)
    background_tasks.add_task(run_transcribe_task, job_id, req)
    label = f"whisper {req.model_size}" if engine == "whisper" else engine
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message=f"Transcription queued (engine: {label})",
        created_at=datetime.now().isoformat()
    )
