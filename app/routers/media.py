import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from datetime import datetime

from app.models.schemas import DownloadRequest, ClipRequest, JobResponse, JobStatus
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService
from app.services.ffmpeg_service import FFmpegService
from app.config import settings, normalize_path

router = APIRouter(prefix="/api/v1", tags=["Media Operations"])

def run_download_task(job_id: str, req: DownloadRequest):
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        def progress(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                downloaded = d.get("downloaded_bytes", 0)
                percent = round(downloaded / max(total, 1) * 100, 1)
                speed_mb = (d.get("speed") or 0) / (1024 * 1024)
                eta = d.get("eta")
                job_manager.update_job(
                    job_id,
                    progress_percent=percent,
                    speed=f"{speed_mb:.2f} MB/s",
                    eta=f"{eta}s" if eta else None
                )

        info = YtDlpService.download(
            url=req.url,
            audio_only=req.audio_only,
            target_format=req.format.value,
            progress_hook=progress
        )
        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            output_files={"media": info["file_path"]}
        )
    except Exception as e:
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))

def run_clip_task(job_id: str, req: ClipRequest):
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        # If source is a URL, download it first
        if req.source.startswith("http://") or req.source.startswith("https://"):
            info = YtDlpService.download(req.source)
            source_path = Path(info["file_path"])
        else:
            source_path = normalize_path(req.source)

        if not source_path.exists():
            raise FileNotFoundError(f"Source media not found: {source_path}")

        clipped_file = FFmpegService.clip_media(
            source_path=source_path,
            start_time=req.start_time,
            end_time=req.end_time,
            target_format=req.format.value,
            re_encode=req.re_encode
        )
        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            output_files={"clip": str(clipped_file)}
        )
    except Exception as e:
        job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))

@router.post("/download", response_model=JobResponse)
def download_media(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = job_manager.create_job("download", req.url)
    background_tasks.add_task(run_download_task, job_id, req)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message="Download queued successfully",
        created_at=datetime.now().isoformat()
    )

@router.post("/clip", response_model=JobResponse)
def clip_media(req: ClipRequest, background_tasks: BackgroundTasks):
    job_id = job_manager.create_job("clip", req.source)
    background_tasks.add_task(run_clip_task, job_id, req)
    return JobResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message=f"Clip extraction queued ({req.start_time} -> {req.end_time})",
        created_at=datetime.now().isoformat()
    )
