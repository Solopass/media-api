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
