import json
import logging
import uuid
import time
import threading
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from concurrent.futures import Future, ThreadPoolExecutor

from app.config import settings
from app.models.schemas import JobStatus, JobDetail

logger = logging.getLogger("media_api")

# Progress ticks arrive several times a second from the yt-dlp hook. The jobs
# file lives on /mnt/d (DrvFs), so persisting every tick is both slow and
# pointless: only status transitions have to survive a restart.
SAVE_THROTTLE_SECONDS = 2.0

class JobQueueManager:
    _instance = None
    _singleton_lock = threading.Lock()

    def __init__(self):
        self.jobs: Dict[str, JobDetail] = {}
        # Single worker: GPU-bound pipelines (ingest, transcribe) run strictly
        # one at a time, so a batch cannot fight itself for VRAM.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._last_activity: float = time.time()
        self._active_pipelines: int = 0
        self._last_save: float = 0.0
        self._storage_file: Path = settings.cache_dir / "media_jobs.json"
        self._load_jobs()

    def submit_pipeline(self, fn: Callable[..., Any], *args: Any) -> Future:
        """Queue a GPU-bound pipeline on the single background worker (FIFO)."""
        return self.executor.submit(fn, *args)

    def shutdown_executor(self):
        """Drop queued pipelines so the process can exit promptly."""
        self.executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def get_instance(cls):
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = JobQueueManager()
            return cls._instance

    def _load_jobs(self):
        """Restore jobs from disk cache across systemd socket activations."""
        try:
            if self._storage_file.exists():
                with open(self._storage_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                for item in raw_data:
                    try:
                        job = JobDetail(**item)
                        # If server died while a job was running, mark it interrupted
                        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                            job.status = JobStatus.FAILED
                            job.error = "Interrupted by prior server shutdown/restart"
                            job.updated_at = datetime.now().isoformat()
                        self.jobs[job.job_id] = job
                    except Exception:
                        continue
                logger.info(f"[queue] Restored {len(self.jobs)} job(s) from {self._storage_file}")
        except Exception as e:
            logger.warning(f"[queue] Failed to load jobs from {self._storage_file}: {e}")

    def _save_jobs(self, force: bool = False):
        """
        Persist jobs atomically to disk.

        Throttled to one write per SAVE_THROTTLE_SECONDS unless forced, and the
        file write happens outside self._lock so progress updates never block
        status polling. Callers must NOT already hold self._lock.
        """
        now = time.time()
        with self._lock:
            if not force and (now - self._last_save) < SAVE_THROTTLE_SECONDS:
                return
            self._last_save = now
            data = [j.model_dump() for j in self.jobs.values()]

        with self._io_lock:
            try:
                settings.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp_file = self._storage_file.with_suffix(".tmp")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                tmp_file.replace(self._storage_file)
            except Exception as e:
                logger.warning(f"[queue] Failed to persist jobs to {self._storage_file}: {e}")

    def pipeline_started(self):
        with self._lock:
            self._active_pipelines += 1
            self._last_activity = time.time()

    def pipeline_ended(self):
        with self._lock:
            self._active_pipelines = max(0, self._active_pipelines - 1)
            self._last_activity = time.time()

    def is_pipeline_active(self) -> bool:
        """Strict check if any pipeline worker is actively running."""
        with self._lock:
            if self._active_pipelines > 0:
                return True
            return any(j.status in (JobStatus.QUEUED, JobStatus.RUNNING) for j in self.jobs.values())

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
        self._save_jobs(force=True)
        return job_id

    def update_job(self, job_id: str, **kwargs):
        status_changed = False
        with self._lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                if job.status == JobStatus.CANCELLED:
                    return
                status_changed = "status" in kwargs and kwargs["status"] != job.status
                for k, v in kwargs.items():
                    if hasattr(job, k):
                        setattr(job, k, v)
                job.updated_at = datetime.now().isoformat()
                self._prune_old_jobs()
            self._last_activity = time.time()
        # Progress-only ticks are throttled; status transitions always persist.
        self._save_jobs(force=status_changed)

    def _prune_old_jobs(self, max_history: int = 100):
        """Keep memory bounded by pruning old completed/failed jobs."""
        if len(self.jobs) > max_history:
            terminal_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
            terminal_keys = [k for k, v in self.jobs.items() if v.status in terminal_statuses]
            for k in terminal_keys[:len(self.jobs) - max_history]:
                self.jobs.pop(k, None)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued or running job."""
        cancelled = False
        with self._lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    job.status = JobStatus.CANCELLED
                    job.error = "Job cancelled by user"
                    job.updated_at = datetime.now().isoformat()
                    self._last_activity = time.time()
                    cancelled = True
        if cancelled:
            self._save_jobs(force=True)
        return cancelled

    def is_cancelled(self, job_id: str) -> bool:
        """Check if a job has been cancelled."""
        with self._lock:
            job = self.jobs.get(job_id)
            return job is not None and job.status == JobStatus.CANCELLED

    def clear_completed_jobs(self) -> int:
        """Clear all terminal (completed, failed, cancelled) jobs from memory."""
        with self._lock:
            terminal_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
            to_remove = [k for k, v in self.jobs.items() if v.status in terminal_statuses]
            for k in to_remove:
                self.jobs.pop(k, None)
            self._last_activity = time.time()
        self._save_jobs(force=True)
        return len(to_remove)

    def get_job(self, job_id: str) -> Optional[JobDetail]:
        with self._lock:
            return self.jobs.get(job_id)

    def list_jobs(self) -> list[JobDetail]:
        with self._lock:
            return list(reversed(list(self.jobs.values())))

job_manager = JobQueueManager.get_instance()
