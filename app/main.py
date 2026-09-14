import os
import sys
import time
import signal
import shutil
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.queue.manager import job_manager
from app.models.schemas import ShutdownRequest, ShutdownResponse
from app.routers import media, transcribe, ingest, jobs

logger = logging.getLogger("media_api")

async def idle_watchdog():
    """Monitors activity and automatically shuts down when idle to free all host RAM and threads."""
    if not settings.auto_close or settings.idle_timeout_seconds <= 0:
        logger.info("[watchdog] Auto-close is disabled.")
        return

    logger.info(
        f"[watchdog] Active: Server will auto-close after {settings.idle_timeout_seconds}s "
        f"of inactivity with 0 active jobs."
    )
    while True:
        try:
            await asyncio.sleep(5)
            active_jobs = job_manager.get_active_job_count()
            idle_duration = time.time() - job_manager.get_last_activity_time()
            if active_jobs == 0 and idle_duration >= settings.idle_timeout_seconds:
                logger.info(
                    f"[*] Idle timeout reached ({int(idle_duration)}s with 0 active jobs). "
                    "Performing graceful auto-shutdown to release system resources..."
                )
                os.kill(os.getpid(), signal.SIGTERM)
                break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[watchdog] Error in watchdog loop: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    watchdog_task = asyncio.create_task(idle_watchdog())
    yield
    # Graceful Shutdown
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Unified Linux Media API (Downloader, Clipper & Whisper Ingestion Pipeline - On Demand)",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def track_activity_middleware(request: Request, call_next):
    # Passive health pings do not postpone the auto-close watchdog
    if not request.url.path.endswith("/health"):
        job_manager.mark_activity()
    response = await call_next(request)
    return response

app.include_router(media.router)
app.include_router(transcribe.router)
app.include_router(ingest.router)
app.include_router(jobs.router)

@app.get("/api/v1/health")
def health_check():
    d_stat = shutil.disk_usage("/mnt/d")
    active_jobs = job_manager.get_active_job_count()
    idle_sec = int(time.time() - job_manager.get_last_activity_time())
    return {
        "status": "online",
        "workstation": "GAMINGPC",
        "app": settings.app_name,
        "version": settings.app_version,
        "hardware_affinity": "E-Cores 8-15 (nice +10)",
        "host_p_cores_protected": True,
        "active_jobs": active_jobs,
        "idle_seconds": idle_sec,
        "auto_close_enabled": settings.auto_close,
        "idle_timeout_seconds": settings.idle_timeout_seconds,
        "storage": {
            "d_drive_free_gb": round(d_stat.free / (1024**3), 2),
            "c_drive_protected_zero_leak": True
        }
    }

@app.post("/api/v1/shutdown", response_model=ShutdownResponse, tags=["Lifecycle"])
async def shutdown_server(req: ShutdownRequest = ShutdownRequest()):
    """Gracefully terminate the server immediately to release all workstation resources."""
    active_jobs = job_manager.get_active_job_count()
    if active_jobs > 0 and not req.force:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot shut down: {active_jobs} job(s) are currently running or queued. Use force=True to override."
        )

    async def _delayed_shutdown():
        await asyncio.sleep(0.5)
        logger.info("[*] Explicit shutdown triggered. Exiting...")
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_shutdown())
    return ShutdownResponse(
        message="Shutdown scheduled successfully.",
        active_jobs=active_jobs,
        scheduled=True
    )

@app.get("/")
def root():
    return {
        "service": settings.app_name,
        "docs": "/docs",
        "health": "/api/v1/health",
        "api_v1": [
            "/api/v1/download",
            "/api/v1/clip",
            "/api/v1/transcribe",
            "/api/v1/ingest",
            "/api/v1/status/{id}",
            "/api/v1/jobs",
            "/api/v1/shutdown"
        ]
    }

