from fastapi import APIRouter, HTTPException
from app.queue.manager import job_manager
from app.models.schemas import JobDetail

router = APIRouter(prefix="/api/v1", tags=["Jobs & Queue Telemetry"])

@router.get("/status/{job_id}", response_model=JobDetail)
def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found")
    return job

@router.get("/jobs", response_model=list[JobDetail])
def list_jobs():
    return job_manager.list_jobs()

@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    cancelled = job_manager.cancel_job(job_id)
    if not cancelled:
        job = job_manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job ID not found")
        raise HTTPException(status_code=400, detail=f"Job cannot be cancelled (current status: {job.status})")
    return {"status": "ok", "message": f"Job {job_id} cancelled", "job_id": job_id}

@router.post("/jobs/clear")
@router.delete("/jobs")
def clear_jobs():
    cleared_count = job_manager.clear_completed_jobs()
    return {"status": "ok", "cleared_count": cleared_count}
