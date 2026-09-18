import json
import time
from pathlib import Path

import pytest
from unittest import mock
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import IngestRequest, JobStatus
from app.queue.manager import JobQueueManager, SAVE_THROTTLE_SECONDS
from app.routers import ingest as ingest_mod

client = TestClient(app)


# --------------------------------------------------------------------------
# Source video cleanup
# --------------------------------------------------------------------------

def test_keep_video_defaults_to_false():
    req = IngestRequest(source="https://example.com/v")
    assert req.keep_video is False


def test_is_managed_media_only_matches_output_dirs(tmp_path, monkeypatch):
    managed_dir = tmp_path / "Videos"
    managed_dir.mkdir()
    monkeypatch.setattr(ingest_mod, "MANAGED_MEDIA_DIRS", (managed_dir,))

    inside = managed_dir / "clip.mp4"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "my_own_movie.mp4"
    outside.write_text("x", encoding="utf-8")

    assert ingest_mod._is_managed_media(inside) is True
    assert ingest_mod._is_managed_media(outside) is False


def test_discard_source_video_removes_downloaded_video(tmp_path, monkeypatch):
    managed_dir = tmp_path / "Videos"
    managed_dir.mkdir()
    monkeypatch.setattr(ingest_mod, "MANAGED_MEDIA_DIRS", (managed_dir,))

    video = managed_dir / "talk.mp4"
    video.write_bytes(b"0" * 2048)
    audio = managed_dir / "talk.mp3"
    audio.write_bytes(b"0" * 64)

    removed, freed_mb = ingest_mod._discard_source_video([video, audio], keep=audio)

    assert removed == [str(video)]
    assert freed_mb == pytest.approx(2048 / (1024 * 1024), abs=0.01)
    assert not video.exists()
    # Audio and transcript sidecars power the UI player, so they survive.
    assert audio.exists()


def test_discard_source_video_never_touches_user_files(tmp_path, monkeypatch):
    managed_dir = tmp_path / "Videos"
    managed_dir.mkdir()
    monkeypatch.setattr(ingest_mod, "MANAGED_MEDIA_DIRS", (managed_dir,))

    user_file = tmp_path / "wedding_footage.mp4"
    user_file.write_bytes(b"0" * 128)

    removed, freed_mb = ingest_mod._discard_source_video([user_file], keep=tmp_path / "nothing.mp3")

    assert removed == []
    assert freed_mb == 0.0
    assert user_file.exists()


def test_discard_source_video_survives_missing_file(tmp_path, monkeypatch):
    managed_dir = tmp_path / "Videos"
    managed_dir.mkdir()
    monkeypatch.setattr(ingest_mod, "MANAGED_MEDIA_DIRS", (managed_dir,))

    ghost = managed_dir / "already_gone.mp4"
    assert ingest_mod._discard_source_video([ghost], keep=managed_dir / "a.mp3") == ([], 0.0)


# --------------------------------------------------------------------------
# Throttled job persistence
# --------------------------------------------------------------------------

@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    return JobQueueManager()


def test_progress_updates_do_not_hit_disk(isolated_manager, tmp_path):
    mgr = isolated_manager
    job_id = mgr.create_job("ingest", "https://example.com/v")
    store = tmp_path / "media_jobs.json"
    assert store.exists(), "creating a job must persist immediately"

    for pct in (10.0, 20.0, 30.0, 40.0):
        mgr.update_job(job_id, progress_percent=pct, speed=f"{pct}%")

    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk[0]["progress_percent"] == 0.0, "progress ticks should be throttled"
    assert mgr.get_job(job_id).progress_percent == 40.0, "in-memory state stays current"


def test_status_transitions_persist_immediately(isolated_manager, tmp_path):
    mgr = isolated_manager
    job_id = mgr.create_job("ingest", "https://example.com/v")
    store = tmp_path / "media_jobs.json"

    mgr.update_job(job_id, status=JobStatus.RUNNING, progress_percent=15.0)
    running = json.loads(store.read_text(encoding="utf-8"))
    assert running[0]["status"] == "running"
    assert running[0]["progress_percent"] == 15.0

    mgr.update_job(job_id, status=JobStatus.COMPLETED, progress_percent=100.0)
    done = json.loads(store.read_text(encoding="utf-8"))
    assert done[0]["status"] == "completed"
    assert done[0]["progress_percent"] == 100.0


def test_throttled_write_lands_after_interval(isolated_manager, tmp_path):
    mgr = isolated_manager
    job_id = mgr.create_job("ingest", "https://example.com/v")
    store = tmp_path / "media_jobs.json"

    mgr._last_save = time.time() - (SAVE_THROTTLE_SECONDS + 1)
    mgr.update_job(job_id, progress_percent=55.0)

    assert json.loads(store.read_text(encoding="utf-8"))[0]["progress_percent"] == 55.0


def test_cancel_persists_and_blocks_later_updates(isolated_manager, tmp_path):
    mgr = isolated_manager
    job_id = mgr.create_job("ingest", "https://example.com/v")
    store = tmp_path / "media_jobs.json"

    assert mgr.cancel_job(job_id) is True
    assert json.loads(store.read_text(encoding="utf-8"))[0]["status"] == "cancelled"

    mgr.update_job(job_id, status=JobStatus.COMPLETED)
    assert mgr.get_job(job_id).status == JobStatus.CANCELLED
    assert mgr.cancel_job(job_id) is False


# --------------------------------------------------------------------------
# Single-worker pipeline queue
# --------------------------------------------------------------------------

def test_ingest_runs_on_the_pipeline_worker():
    with mock.patch("app.routers.ingest.job_manager.submit_pipeline") as submit,          mock.patch("app.routers.ingest.job_manager.create_job", return_value="test-job-id"):
        res = client.post("/api/v1/ingest", json={"source": "https://example.com/v"})
        assert res.status_code == 200
        assert res.json()["status"] == "queued"
        submit.assert_called_once()
        assert submit.call_args[0][0] is ingest_mod.run_ingest_pipeline


def test_pipeline_worker_is_serial(isolated_manager):
    assert isolated_manager.executor._max_workers == 1


# --------------------------------------------------------------------------
# Playlist bounds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_max", [0, -5, 501])
def test_expand_playlist_rejects_out_of_range_max_items(bad_max):
    res = client.post("/api/v1/batch/expand-playlist", json={
        "url": "https://www.youtube.com/playlist?list=PL1",
        "max_items": bad_max
    })
    assert res.status_code == 422


# --------------------------------------------------------------------------
# End-to-end pipeline: real ffmpeg + real note writer, stubbed STT/LLM
# --------------------------------------------------------------------------

def _make_test_video(path):
    """Render a 1s silent-ish test clip with ffmpeg; returns False if unavailable."""
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    """Redirect every output path into tmp so the real vault is untouched."""
    for name in ("output_videos", "output_audio", "output_transcripts",
                 "vault_transcripts", "uploads_dir", "cache_dir"):
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, name, target)
    monkeypatch.setattr(settings, "vault_dashboard", tmp_path / "Dashboard.md")
    monkeypatch.setattr(
        ingest_mod, "MANAGED_MEDIA_DIRS",
        (settings.output_videos, settings.output_audio, settings.uploads_dir)
    )

    fake_transcript = {
        "full_text": "This is a stubbed transcript for the cleanup test.",
        "segments": [{"start": 0.0, "end": 1.0, "text": "Stubbed segment"}],
        "duration": 1.0,
        "model": "stub",
        "language": "en"
    }
    monkeypatch.setattr(ingest_mod.stt_service, "transcribe", lambda *a, **k: fake_transcript)
    monkeypatch.setattr(ingest_mod.stt_service, "resolve_engine", lambda *a, **k: "stub")
    monkeypatch.setattr(ingest_mod, "summarize_with_ollama", lambda **k: "Stubbed summary.")
    monkeypatch.setattr(ingest_mod, "_trigger_reindex", lambda: None)
    # Isolated manager so test jobs never land in the real Job Monitor history.
    monkeypatch.setattr(ingest_mod, "job_manager", JobQueueManager())
    return tmp_path


@pytest.mark.parametrize("keep_video,should_survive", [(False, False), (True, True)])
def test_pipeline_honours_keep_video(pipeline_env, keep_video, should_survive):
    video = settings.output_videos / "pipeline_probe.mp4"
    if not _make_test_video(video):
        pytest.skip("ffmpeg unavailable")

    job_id = ingest_mod.job_manager.create_job("ingest", str(video))
    req = IngestRequest(source=str(video), summarize=True, keep_video=keep_video)
    ingest_mod.run_ingest_pipeline(job_id, req)

    job = ingest_mod.job_manager.get_job(job_id)
    assert job.status == JobStatus.COMPLETED, job.error

    # The vault note always lands, whatever the retention setting.
    note = settings.vault_transcripts / Path(job.output_files["vault_note"]).name
    assert note.exists()
    assert "Stubbed summary." in note.read_text(encoding="utf-8")

    # Audio + transcript sidecar are kept either way (the UI player streams them).
    assert Path(job.output_files["audio"]).exists()
    assert Path(job.output_files["transcript_json"]).exists()

    assert video.exists() is should_survive
    if should_survive:
        assert job.output_files["media"] == str(video)
    else:
        assert "media" not in job.output_files
        assert "source video deleted" in job.speed
