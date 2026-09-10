import uuid
import time
import threading
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import JobStatus, JobDetail

class JobQueueManager:
    _instance = None
    _singleton_lock = threading.Lock()

    def __init__(self):
        self.jobs: Dict[str, JobDetail] = {}
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._lock = threading.Lock()
        self._last_activity: float = time.time()

    @classmethod
    def get_instance(cls):
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = JobQueueManager()
            return cls._instance

    def mark_activity(self):
        """Update last activity timestamp whenever a request or job step occurs."""
        with self._lock:
            self._last_activity = time.time()

    def get_last_activity_time(self) -> float:
        with self._lock:
            return self._last_activity

    def get_active_job_count(self) -> int:
        """Return the number of jobs currently queued or running."""
        with self._lock:
            return sum(
                1 for j in self.jobs.values()
                if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            )

    def create_job(self, job_type: str, source: str) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        job = JobDetail(
            job_id=job_id,
            type=job_type,
            source=source,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now
        )
        with self._lock:
            self.jobs[job_id] = job
            self._last_activity = time.time()
        return job_id

    def update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                for k, v in kwargs.items():
                    if hasattr(job, k):
                        setattr(job, k, v)
                job.updated_at = datetime.now().isoformat()
                self._prune_old_jobs()
            self._last_activity = time.time()

    def _prune_old_jobs(self, max_history: int = 100):
        """Keep memory bounded by pruning old completed/failed jobs."""
        if len(self.jobs) > max_history:
            terminal_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
            terminal_keys = [k for k, v in self.jobs.items() if v.status in terminal_statuses]
            for k in terminal_keys[:len(self.jobs) - max_history]:
                self.jobs.pop(k, None)

    def get_job(self, job_id: str) -> Optional[JobDetail]:
        with self._lock:
            return self.jobs.get(job_id)

    def list_jobs(self) -> list[JobDetail]:
        with self._lock:
            return list(reversed(list(self.jobs.values())))

job_manager = JobQueueManager.get_instance()
