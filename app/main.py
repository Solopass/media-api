import os
import sys
import time
import signal
import shutil
import asyncio
import json
import urllib.request
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.config import settings
from app.queue.manager import job_manager
from app.models.schemas import ShutdownRequest, ShutdownResponse, ConfigIdleRequest
from app.routers import media, transcribe, ingest, jobs, upload
from app.services.updater_service import UpdaterService
from app.services.gpu_service import GpuService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("media_api")

async def idle_watchdog():
    """Monitors activity and automatically shuts down when idle to free all host RAM and threads."""
    logger.info(
        f"[watchdog] Started: auto_close={settings.auto_close}, "
        f"timeout={settings.idle_timeout_seconds}s."
    )
    while True:
        try:
            await asyncio.sleep(5)
            if not settings.auto_close or settings.idle_timeout_seconds <= 0:
                continue

            active_jobs = job_manager.get_active_job_count()
            is_active = job_manager.is_pipeline_active()

            # Strictly NEVER shut down if a job is queued or running or pipeline is active
            if active_jobs > 0 or is_active:
                continue

            idle_duration = time.time() - job_manager.get_last_activity_time()
            if idle_duration >= settings.idle_timeout_seconds:
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
    updater_task = asyncio.create_task(UpdaterService.background_auto_update())
    yield
    # Graceful Shutdown
    job_manager.shutdown_executor()
    watchdog_task.cancel()
    updater_task.cancel()
    try:
        await asyncio.gather(watchdog_task, updater_task, return_exceptions=True)
    except Exception:
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
app.include_router(upload.router)

static_dir = settings.workspace_dir / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/api/v1/health")
@app.get("/health", include_in_schema=False)
def health_check():
    d_stat = shutil.disk_usage("/mnt/d")
    active_jobs = job_manager.get_active_job_count()
    idle_sec = int(time.time() - job_manager.get_last_activity_time())
    return {
        "status": "online",
        "workstation": "SOL",
        "app": settings.app_name,
        "version": settings.app_version,
        "hardware_affinity": "E-Cores (nice +10)",
        "host_p_cores_protected": True,
        "active_jobs": active_jobs,
        "idle_seconds": idle_sec,
        "auto_close_enabled": settings.auto_close,
        "idle_timeout_seconds": settings.idle_timeout_seconds,
        "ytdlp_version": UpdaterService.get_installed_version(),
        "ytdlp_last_update_check": UpdaterService.get_last_update_time(),
        "auto_update_ytdlp": settings.auto_update_ytdlp,
        "storage": {
            "d_drive_free_gb": round(d_stat.free / (1024**3), 2),
            "c_drive_protected_zero_leak": True
        }
    }

@app.get("/api/v1/models", tags=["Local LLM"])
def list_local_models():
    """Fetch currently installed Ollama models with VRAM fit evaluation for the UI dropdown."""
    gpu_stats = GpuService.get_vram_usage()
    model_details = []
    model_names = []
    
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                name = m.get("name")
                size = m.get("size", 0)
                eval_res = GpuService.evaluate_model_fit(name, size, gpu_stats)
                model_names.append(name)
                model_details.append({
                    "name": name,
                    "size_gb": round(size / (1024**3), 1) if size else 0,
                    **eval_res
                })
    except Exception as e:
        logger.warning(f"Failed to query Ollama tags: {e}")
        fallback_names = ["sol-fast:latest", "sol-vision:latest", "sol-specialist:latest", "sol-coder:latest"]
        for name in fallback_names:
            model_names.append(name)
            model_details.append({
                "name": name,
                "size_gb": 12.4,
                **GpuService.evaluate_model_fit(name, None, gpu_stats)
            })

    def_name = settings.default_llm_model
    matched_default = next((n for n in model_names if n == def_name or n.startswith(f"{def_name}:")), def_name)

    return {
        "models": model_names,
        "model_details": model_details,
        "default": matched_default,
        "gpu": gpu_stats
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

@app.post("/api/v1/config/idle-timeout", tags=["System & Lifecycle"])
def update_idle_timeout(req: ConfigIdleRequest):
    """Dynamically adjust idle shutdown timeout and auto-close state."""
    settings.auto_close = req.auto_close
    settings.idle_timeout_seconds = req.timeout_seconds
    job_manager.mark_activity()
    return {
        "status": "ok",
        "auto_close": settings.auto_close,
        "idle_timeout_seconds": settings.idle_timeout_seconds,
        "message": f"Idle timeout updated to {settings.idle_timeout_seconds}s (auto_close: {settings.auto_close})"
    }

@app.get("/ui", tags=["GUI"])
def web_ui():
    index_file = settings.workspace_dir / "app" / "static" / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    raise HTTPException(status_code=404, detail="UI index.html not found")

@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept", "")
    index_file = settings.workspace_dir / "app" / "static" / "index.html"
    # Serve UI for web browsers navigating to root
    if "text/html" in accept and index_file.exists():
        return FileResponse(str(index_file))
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
            "/api/v1/shutdown",
            "/api/v1/models"
        ]
    }

