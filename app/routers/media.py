import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from datetime import datetime

from app.models.schemas import DownloadRequest, ClipRequest, JobResponse, JobStatus, MediaProbeResponse
from app.queue.manager import job_manager
from app.services.ytdlp_service import YtDlpService, x_friendly_error, x_refusal_message, x_url_kind
from app.services.ffmpeg_service import FFmpegService
from app.config import settings, normalize_path
from app.services.updater_service import UpdaterService

router = APIRouter(prefix="/api/v1", tags=["Media Operations"])

import logging

logger = logging.getLogger("media_api")

def run_download_task(job_id: str, req: DownloadRequest):
    if job_manager.is_cancelled(job_id):
        return
    job_manager.pipeline_started()
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        if job_manager.is_cancelled(job_id):
            return

        def progress(d):
            if job_manager.is_cancelled(job_id):
                return
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
            progress_hook=progress,
            quality=req.quality.value
        )

        if job_manager.is_cancelled(job_id):
            return

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            output_files={"media": info["file_path"]}
        )
    except Exception as e:
        logger.exception(f"[media] Download task failed for job {job_id}: {e}")
        if not job_manager.is_cancelled(job_id):
            job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))
    finally:
        job_manager.pipeline_ended()

def run_clip_task(job_id: str, req: ClipRequest):
    if job_manager.is_cancelled(job_id):
        return
    job_manager.pipeline_started()
    job_manager.update_job(job_id, status=JobStatus.RUNNING)
    try:
        if job_manager.is_cancelled(job_id):
            return

        # If source is a URL, download it first
        if req.source.startswith("http://") or req.source.startswith("https://"):
            info = YtDlpService.download(req.source, quality=req.quality.value)
            source_path = Path(info["file_path"])
        else:
            source_path = normalize_path(req.source)

        if job_manager.is_cancelled(job_id):
            return

        if not source_path.exists():
            raise FileNotFoundError(f"Source media not found: {source_path}")

        clipped_file = FFmpegService.clip_media(
            source_path=source_path,
            start_time=req.start_time,
            end_time=req.end_time,
            target_format=req.format.value,
            re_encode=req.re_encode,
            normalize_audio=req.normalize_audio,
            speech_optimized=req.speech_optimized
        )

        if job_manager.is_cancelled(job_id):
            return

        job_manager.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress_percent=100.0,
            output_files={"clip": str(clipped_file)}
        )
    except Exception as e:
        logger.exception(f"[media] Clip task failed for job {job_id}: {e}")
        if not job_manager.is_cancelled(job_id):
            job_manager.update_job(job_id, status=JobStatus.FAILED, error=str(e))
    finally:
        job_manager.pipeline_ended()

@router.get("/media/probe", response_model=MediaProbeResponse)
def probe_media_info(url: str = Query(..., description="Target video/audio URL or local file path to probe")):
    """Extract metadata preview (title, uploader, duration, thumbnail) without full download."""
    x_refusal = x_refusal_message(x_url_kind(url))
    if x_refusal:
        raise HTTPException(status_code=400, detail=x_refusal)
    try:
        if url.startswith("http://") or url.startswith("https://"):
            data = YtDlpService.probe_url(url)
        else:
            path = normalize_path(url)
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"Local media file not found: {url}")
            data = FFmpegService.probe_local_media(path)
        return MediaProbeResponse(**data)
    except HTTPException:
        raise
    except Exception as e:
        friendly = x_friendly_error(url, e)
        if friendly:
            raise HTTPException(status_code=400, detail=friendly)
        raise HTTPException(status_code=400, detail=f"Failed to probe media: {str(e)}")

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

@router.post("/update-ytdlp")
async def update_ytdlp(force: bool = Query(True, description="Force update check even if checked recently")):
    """Check for and apply yt-dlp updates."""
    return await UpdaterService.run_update(force=force)


