import logging
from pathlib import Path

from fastapi import APIRouter, Query

from app.logging_setup import log_file_path, recent_events
from app.models.schemas import ClientLogRequest, ClientLogResponse, DiagnosticsResponse

router = APIRouter(prefix="/api/v1", tags=["Diagnostics"])

logger = logging.getLogger("media_api.client")


@router.post("/client-log", response_model=ClientLogResponse)
def record_client_log(req: ClientLogRequest):
    """
    Record a browser-side failure in the server log.

    Without this, anything that breaks in the UI - a failed fetch when the WSL VM
    is torn down mid-request, a JS exception during a batch - leaves no trace on
    disk, so a later diagnosis has only the user's memory to go on. The UI
    buffers these in localStorage and flushes them once the server answers again.
    """
    detail = f"[{req.context or 'ui'}] {req.message}"
    if req.url:
        detail += f" (at {req.url})"
    if req.stack:
        detail += f"\n{req.stack}"

    if req.level.lower() in ("error", "critical"):
        logger.error(detail)
    elif req.level.lower() == "warning":
        logger.warning(detail)
    else:
        logger.info(detail)

    return ClientLogResponse(recorded=True, level=req.level)


@router.get("/diagnostics", response_model=DiagnosticsResponse)
def get_diagnostics(
    limit: int = Query(50, ge=1, le=200, description="Maximum events to return"),
    level: str = Query("WARNING", description="Minimum level: WARNING or ERROR"),
    tail_lines: int = Query(0, ge=0, le=500, description="Also return this many lines from the log file"),
):
    """Recent warnings and errors from this process, plus the log file location."""
    path = log_file_path()
    tail = []
    if tail_lines:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = [line.rstrip("\n") for line in f.readlines()[-tail_lines:]]
        except Exception as e:
            tail = [f"(could not read {path}: {e})"]

    return DiagnosticsResponse(
        log_file=str(path),
        log_file_exists=path.exists(),
        log_file_size_bytes=path.stat().st_size if path.exists() else 0,
        events=recent_events(limit=limit, level=level),
        tail=tail,
    )
