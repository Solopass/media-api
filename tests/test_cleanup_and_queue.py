import json
import time
from pathlib import Path

import pytest
from unittest import mock
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import IngestRequest, JobStatus
from app.paths import MEDIA_ROOTS, NOTE_ROOTS, is_path_allowed
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


def _run_pipeline(source, keep_video=False):
    job_id = ingest_mod.job_manager.create_job("ingest", str(source))
    req = IngestRequest(source=str(source), summarize=True, keep_video=keep_video)
    ingest_mod.run_ingest_pipeline(job_id, req)
    return ingest_mod.job_manager.get_job(job_id)


def _assert_note_and_audio(job):
    note = settings.vault_transcripts / Path(job.output_files["vault_note"]).name
    assert note.exists()
    assert "Stubbed summary." in note.read_text(encoding="utf-8")
    # Audio + transcript sidecar are kept either way (the UI player streams them).
    assert Path(job.output_files["audio"]).exists()
    assert Path(job.output_files["transcript_json"]).exists()


def test_downloaded_video_is_deleted(pipeline_env, monkeypatch):
    """The common case: yt-dlp fetched it, so the pipeline owns it."""
    downloaded = settings.output_videos / "downloaded.mp4"
    if not _make_test_video(downloaded):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(ingest_mod.YtDlpService, "download", lambda *a, **k: {
        "title": "Test Clip",
        "duration": 1,
        "uploader": "Test",
        "file_path": str(downloaded),
        "url": "https://example.com/v"
    })

    job = _run_pipeline("https://example.com/v")

    assert job.status == JobStatus.COMPLETED, job.error
    _assert_note_and_audio(job)
    assert not downloaded.exists()
    assert "media" not in job.output_files
    assert "source video deleted" in job.speed


def test_downloaded_video_is_kept_when_requested(pipeline_env, monkeypatch):
    downloaded = settings.output_videos / "downloaded_keep.mp4"
    if not _make_test_video(downloaded):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(ingest_mod.YtDlpService, "download", lambda *a, **k: {
        "title": "Test Clip",
        "duration": 1,
        "uploader": "Test",
        "file_path": str(downloaded),
        "url": "https://example.com/v"
    })

    job = _run_pipeline("https://example.com/v", keep_video=True)

    assert job.status == JobStatus.COMPLETED, job.error
    _assert_note_and_audio(job)
    assert downloaded.exists()
    assert job.output_files["media"] == str(downloaded)


def test_upload_cache_copy_is_deleted(pipeline_env):
    """A drag-and-drop copy in the uploads cache is ours to clean up."""
    uploaded = settings.uploads_dir / "dropped.mp4"
    if not _make_test_video(uploaded):
        pytest.skip("ffmpeg unavailable")

    job = _run_pipeline(uploaded)

    assert job.status == JobStatus.COMPLETED, job.error
    _assert_note_and_audio(job)
    assert not uploaded.exists()


def test_preexisting_file_in_output_dir_survives(pipeline_env):
    """
    Re-ingesting a file the user parked in the output directory must not delete
    it: this job did not produce it, wherever it happens to live.
    """
    parked = settings.output_videos / "my_own_copy.mp4"
    if not _make_test_video(parked):
        pytest.skip("ffmpeg unavailable")

    job = _run_pipeline(parked)

    assert job.status == JobStatus.COMPLETED, job.error
    _assert_note_and_audio(job)
    assert parked.exists()
    assert job.output_files["media"] == str(parked)


def test_failed_job_reclaims_its_download(pipeline_env, monkeypatch):
    downloaded = settings.output_videos / "doomed.mp4"
    if not _make_test_video(downloaded):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(ingest_mod.YtDlpService, "download", lambda *a, **k: {
        "title": "Test Clip",
        "duration": 1,
        "uploader": "Test",
        "file_path": str(downloaded),
        "url": "https://example.com/v"
    })

    def _boom(*a, **k):
        raise RuntimeError("stt exploded")

    monkeypatch.setattr(ingest_mod.stt_service, "transcribe", _boom)

    job = _run_pipeline("https://example.com/v")

    assert job.status == JobStatus.FAILED
    assert not downloaded.exists(), "an aborted run must not hoard the download"
    # The error note must not advertise a file that was just reclaimed.
    assert str(downloaded) not in job.output_files.values()


def test_cancelled_job_reclaims_its_download(pipeline_env, monkeypatch):
    downloaded = settings.output_videos / "cancelled.mp4"
    if not _make_test_video(downloaded):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(ingest_mod.YtDlpService, "download", lambda *a, **k: {
        "title": "Test Clip",
        "duration": 1,
        "uploader": "Test",
        "file_path": str(downloaded),
        "url": "https://example.com/v"
    })

    job_id = ingest_mod.job_manager.create_job("ingest", "https://example.com/v")

    # Cancel from inside transcription, the way the UI Cancel button would.
    def _cancel_midway(*a, **k):
        ingest_mod.job_manager.cancel_job(job_id)
        return {
            "full_text": "partial",
            "segments": [{"start": 0.0, "end": 1.0, "text": "partial"}],
            "duration": 1.0,
            "model": "stub",
            "language": "en"
        }

    monkeypatch.setattr(ingest_mod.stt_service, "transcribe", _cancel_midway)
    ingest_mod.run_ingest_pipeline(job_id, IngestRequest(source="https://example.com/v"))

    job = ingest_mod.job_manager.get_job(job_id)
    assert job.status == JobStatus.CANCELLED
    assert not downloaded.exists()


def test_keep_video_survives_cancellation(pipeline_env, monkeypatch):
    downloaded = settings.output_videos / "cancelled_keep.mp4"
    if not _make_test_video(downloaded):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(ingest_mod.YtDlpService, "download", lambda *a, **k: {
        "title": "Test Clip",
        "duration": 1,
        "uploader": "Test",
        "file_path": str(downloaded),
        "url": "https://example.com/v"
    })

    job_id = ingest_mod.job_manager.create_job("ingest", "https://example.com/v")
    ingest_mod.job_manager.cancel_job(job_id)
    ingest_mod.run_ingest_pipeline(
        job_id, IngestRequest(source="https://example.com/v", keep_video=True)
    )

    assert downloaded.exists()


# --------------------------------------------------------------------------
# Path allow-lists
# --------------------------------------------------------------------------

def test_resummarize_rejects_paths_outside_the_vault(tmp_path):
    outsider = tmp_path / "secrets.md"
    outsider.write_text("# not in the vault", encoding="utf-8")

    res = client.post("/api/v1/vault/resummarize", json={"note_path": str(outsider)})
    assert res.status_code == 403
    assert outsider.read_text(encoding="utf-8") == "# not in the vault"


def test_resummarize_rejects_non_markdown_targets():
    target = settings.vault_transcripts / "resummarize_probe.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("plain text, not a note", encoding="utf-8")
    try:
        res = client.post("/api/v1/vault/resummarize", json={"note_path": str(target)})
        assert res.status_code == 400
        assert target.read_text(encoding="utf-8") == "plain text, not a note"
    finally:
        target.unlink(missing_ok=True)


def test_media_roots_exclude_the_cache_root():
    assert is_path_allowed(settings.uploads_dir / "clip.mp4", MEDIA_ROOTS) is True
    assert is_path_allowed(Path("/mnt/d/Output/Audio/clip.mp3"), MEDIA_ROOTS) is True
    # cookies.txt and agent session history live in the cache root.
    assert is_path_allowed(Path("/mnt/d/AI/Cache/cookies.txt"), MEDIA_ROOTS) is False
    assert is_path_allowed(Path("/etc/passwd"), MEDIA_ROOTS) is False


def test_note_roots_reject_traversal():
    escape = Path("/mnt/d/AI/Vault/Transcripts/../../../etc/passwd")
    assert is_path_allowed(escape, NOTE_ROOTS) is False
