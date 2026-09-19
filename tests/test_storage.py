import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import JobStatus
from app.queue.manager import job_manager
from app.services import storage_service

client = TestClient(app)

DAY = 86400


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    """Redirect every scanned directory into tmp and clear the scan cache."""
    for name in ("output_videos", "output_audio", "output_transcripts", "uploads_dir"):
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, name, target)
    storage_service.invalidate_cache()
    yield tmp_path
    storage_service.invalidate_cache()


def _make_file(path: Path, size_bytes: int = 1024, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size_bytes)
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def _category(scan, name):
    return next(c for c in scan["categories"] if c["name"] == name)


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def test_scan_categorises_files(storage_env):
    _make_file(settings.output_videos / "clip.mp4", 2048)
    _make_file(settings.output_audio / "clip.mp3", 4096)
    _make_file(settings.output_audio / "clip.transcript.json", 256)
    _make_file(settings.output_transcripts / "note.md", 128)
    _make_file(settings.uploads_dir / "dropped.mp4", 512)

    scan = storage_service.scan(max_cache_age=0.0)

    assert _category(scan, "videos")["file_count"] == 1
    assert _category(scan, "videos")["total_bytes"] == 2048
    assert _category(scan, "audio")["file_count"] == 1
    assert _category(scan, "audio")["total_bytes"] == 4096
    assert _category(scan, "sidecars")["file_count"] == 1
    assert _category(scan, "note_mirrors")["file_count"] == 1
    assert _category(scan, "uploads")["file_count"] == 1
    # Each file counted exactly once.
    assert scan["total_bytes"] == 2048 + 4096 + 256 + 128 + 512


def test_scan_marks_only_media_reclaimable(storage_env):
    scan = storage_service.scan(max_cache_age=0.0)
    flags = {c["name"]: c["reclaimable"] for c in scan["categories"]}
    assert flags == {
        "videos": True, "audio": True, "uploads": True,
        "sidecars": False, "note_mirrors": False,
    }


def test_scan_handles_empty_directories(storage_env):
    scan = storage_service.scan(max_cache_age=0.0)
    assert scan["total_bytes"] == 0
    assert all(c["file_count"] == 0 for c in scan["categories"])
    assert all(c["oldest_modified"] is None for c in scan["categories"])


def test_scan_age_buckets_count_only_old_media(storage_env):
    _make_file(settings.output_audio / "fresh.mp3", 1024, age_days=1)
    _make_file(settings.output_audio / "old.mp3", 2048, age_days=45)
    _make_file(settings.output_audio / "old.transcript.json", 512, age_days=45)

    scan = storage_service.scan(max_cache_age=0.0)

    assert scan["reclaimable_by_age"]["7"]["file_count"] == 1
    assert scan["reclaimable_by_age"]["7"]["bytes"] == 2048
    assert scan["reclaimable_by_age"]["30"]["file_count"] == 1
    assert scan["reclaimable_by_age"]["90"]["file_count"] == 0


def test_scan_cache_keys_on_directories(storage_env, tmp_path, monkeypatch):
    _make_file(settings.output_audio / "a.mp3", 1024)
    first = storage_service.scan()
    assert first["total_bytes"] == 1024

    # A different directory set must not read the previous result.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(settings, "output_audio", other)
    assert storage_service.scan()["total_bytes"] == 0


def test_storage_endpoint_shape(storage_env):
    _make_file(settings.output_videos / "clip.mp4", 1024)

    res = client.get("/api/v1/storage?refresh=true")

    assert res.status_code == 200
    data = res.json()
    assert data["drive"]["total_gb"] > 0
    assert {c["name"] for c in data["categories"]} == {
        "videos", "audio", "uploads", "sidecars", "note_mirrors"
    }
    assert data["reclaimable_by_age"]["30"]["days"] == 30
    assert data["scanned_at"]


# --------------------------------------------------------------------------
# Reclaim
# --------------------------------------------------------------------------

def test_dry_run_reports_without_deleting(storage_env):
    old = _make_file(settings.output_audio / "old.mp3", 4096, age_days=45)
    _make_file(settings.output_audio / "fresh.mp3", 1024, age_days=2)

    res = client.post("/api/v1/storage/reclaim", json={"older_than_days": 30, "dry_run": True})

    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is True
    assert data["file_count"] == 1
    assert data["candidates"][0]["path"] == str(old)
    assert data["freed_mb"] == 0.0
    assert old.exists(), "a dry run must not delete anything"


def test_reclaim_removes_only_aged_files(storage_env):
    old_video = _make_file(settings.output_videos / "old.mp4", 8192, age_days=60)
    old_audio = _make_file(settings.output_audio / "old.mp3", 4096, age_days=60)
    fresh = _make_file(settings.output_audio / "fresh.mp3", 1024, age_days=1)

    res = client.post("/api/v1/storage/reclaim", json={"older_than_days": 30, "dry_run": False})

    assert res.status_code == 200
    data = res.json()
    assert data["file_count"] == 2
    assert data["freed_bytes"] == 8192 + 4096
    assert data["freed_mb"] == pytest.approx((8192 + 4096) / (1024 ** 2), abs=0.01)
    assert not old_video.exists()
    assert not old_audio.exists()
    assert fresh.exists()


def test_reclaim_honours_category_selection(storage_env):
    old_video = _make_file(settings.output_videos / "old.mp4", 8192, age_days=60)
    old_upload = _make_file(settings.uploads_dir / "old.mp4", 2048, age_days=60)

    res = client.post("/api/v1/storage/reclaim", json={
        "older_than_days": 30, "categories": ["uploads"], "dry_run": False
    })

    assert res.status_code == 200
    assert res.json()["file_count"] == 1
    assert old_video.exists(), "videos were not selected"
    assert not old_upload.exists()


def test_sidecars_and_notes_are_never_reclaimed(storage_env):
    sidecar = _make_file(settings.output_audio / "old.transcript.json", 512, age_days=400)
    text = _make_file(settings.output_audio / "old.txt", 256, age_days=400)
    note = _make_file(settings.output_transcripts / "old_note.md", 128, age_days=400)
    audio = _make_file(settings.output_audio / "old.mp3", 4096, age_days=400)

    res = client.post("/api/v1/storage/reclaim", json={
        "older_than_days": 0, "categories": ["videos", "audio", "uploads"], "dry_run": False
    })

    assert res.status_code == 200
    assert not audio.exists()
    assert sidecar.exists(), "sidecars power the transcript view and chapter regeneration"
    assert text.exists()
    assert note.exists()


def test_files_of_active_jobs_are_skipped(storage_env):
    in_use = _make_file(settings.output_audio / "in_use.mp3", 4096, age_days=60)
    stale = _make_file(settings.output_audio / "stale.mp3", 4096, age_days=60)

    job_id = job_manager.create_job("ingest", "https://example.com/v")
    job_manager.update_job(
        job_id, status=JobStatus.RUNNING, output_files={"audio": str(in_use)}
    )
    try:
        res = client.post("/api/v1/storage/reclaim", json={
            "older_than_days": 30, "dry_run": False
        })

        assert res.status_code == 200
        assert in_use.exists(), "a running job's files must survive"
        assert not stale.exists()
    finally:
        job_manager.cancel_job(job_id)


def test_completed_job_does_not_protect_files(storage_env):
    done = _make_file(settings.output_audio / "done.mp3", 4096, age_days=60)

    job_id = job_manager.create_job("ingest", "https://example.com/v")
    job_manager.update_job(
        job_id, status=JobStatus.COMPLETED, output_files={"audio": str(done)}
    )

    res = client.post("/api/v1/storage/reclaim", json={"older_than_days": 30, "dry_run": False})

    assert res.status_code == 200
    assert not done.exists()


@pytest.mark.parametrize("categories", [["sidecars"], ["note_mirrors"], ["everything"], []])
def test_protected_and_unknown_categories_are_rejected(storage_env, categories):
    res = client.post("/api/v1/storage/reclaim", json={
        "older_than_days": 30, "categories": categories, "dry_run": True
    })
    assert res.status_code == 422


@pytest.mark.parametrize("days", [-1, 4000])
def test_older_than_days_is_bounded(storage_env, days):
    res = client.post("/api/v1/storage/reclaim", json={"older_than_days": days})
    assert res.status_code == 422


def test_reclaim_ignores_paths_outside_the_scanned_roots(storage_env, tmp_path):
    """A symlink into someone else's directory must not make it a candidate."""
    outsider = tmp_path / "outside" / "precious.mp4"
    _make_file(outsider, 4096, age_days=60)
    link = settings.output_videos / "link.mp4"
    try:
        link.symlink_to(outsider)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    client.post("/api/v1/storage/reclaim", json={"older_than_days": 30, "dry_run": False})

    assert outsider.exists(), "resolution must keep the target out of scope"
