import logging

from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import (
    StorageReclaimRequest, StorageReclaimResponse, StorageResponse
)
from app.services import storage_service

router = APIRouter(prefix="/api/v1", tags=["Storage & Reclaim"])

logger = logging.getLogger("media_api")


@router.get("/storage", response_model=StorageResponse)
def get_storage(
    refresh: bool = Query(False, description="Bypass the short scan cache and re-walk the directories")
):
    """Report what the API's output directories hold, per category, plus D: usage."""
    return storage_service.scan(max_cache_age=0.0 if refresh else 30.0)


@router.post("/storage/reclaim", response_model=StorageReclaimResponse)
def reclaim_storage(req: StorageReclaimRequest):
    """
    Delete old generated media. Defaults to a dry run: the UI previews the
    candidates, then re-posts with dry_run=false to actually remove them.

    Transcript sidecars and markdown notes are never candidates, and files
    belonging to a queued or running job are skipped.
    """
    unknown = [c for c in req.categories if c not in storage_service.RECLAIMABLE]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Not reclaimable: {', '.join(unknown)}. "
                f"Allowed categories: {', '.join(storage_service.RECLAIMABLE)}"
            )
        )
    if not req.categories:
        raise HTTPException(status_code=422, detail="Select at least one category to reclaim")

    if req.dry_run:
        candidates = storage_service.plan_reclaim(req.older_than_days, req.categories)
        total_bytes = sum(c["size_bytes"] for c in candidates)
        return StorageReclaimResponse(
            dry_run=True,
            older_than_days=req.older_than_days,
            categories=req.categories,
            candidates=candidates,
            file_count=len(candidates),
            total_bytes=total_bytes,
            total_mb=round(total_bytes / (1024 ** 2), 2),
        )

    result = storage_service.execute_reclaim(req.older_than_days, req.categories)
    return StorageReclaimResponse(
        dry_run=False,
        older_than_days=req.older_than_days,
        categories=req.categories,
        candidates=result["removed"],
        file_count=result["file_count"],
        total_bytes=result["freed_bytes"],
        total_mb=result["freed_mb"],
        freed_bytes=result["freed_bytes"],
        freed_mb=result["freed_mb"],
        errors=result["errors"],
    )
