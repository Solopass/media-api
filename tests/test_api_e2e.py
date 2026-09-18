import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

client = TestClient(app)

def test_health_check():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert data["workstation"] in ("SOL", "GAMINGPC")
    assert data["host_p_cores_protected"] is True
    assert "d_drive_free_gb" in data["storage"]
    assert "active_jobs" in data
    assert "auto_close_enabled" in data
    assert data["auto_close_enabled"] is True

def test_root_index():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "api_v1" in data
    assert "/api/v1/download" in data["api_v1"]
    assert "/api/v1/shutdown" in data["api_v1"]

def test_job_not_found():
    response = client.get("/api/v1/status/non-existent-job-id")
    assert response.status_code == 404

import unittest.mock as mock

def test_shutdown_endpoint():
    with mock.patch("os.kill") as mock_kill:
        response = client.post("/api/v1/shutdown", json={"force": False})
        assert response.status_code == 200
        data = response.json()
        assert data["scheduled"] is True
        assert "Shutdown scheduled" in data["message"]

def test_download_endpoint_queue():
    with mock.patch("app.routers.media.run_download_task") as mock_task:
        response = client.post("/api/v1/download", json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "format": "mp4",
            "audio_only": False
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert "job_id" in data

def test_clip_endpoint_queue():
    with mock.patch("app.routers.media.run_clip_task") as mock_task:
        response = client.post("/api/v1/clip", json={
            "source": "/mnt/d/Output/Videos/sample.mp4",
            "start_time": "00:00:10",
            "end_time": "00:00:20",
            "format": "mp4"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert "job_id" in data

def test_models_endpoint():
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert "models" in data
    assert "default" in data
    assert data["default"].startswith("sol-fast")

def test_ui_endpoint():
    response = client.get("/ui")
    assert response.status_code == 200
    assert "SOL Media Studio" in response.text

def test_upload_endpoint():
    from pathlib import Path
    file_bytes = b"fake-audio-content-for-testing"
    files = {"file": ("test_upload_audio.mp3", file_bytes, "audio/mpeg")}
    response = client.post("/api/v1/upload", files=files)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "test_upload_audio.mp3" in data["filename"]
    assert "file_path" in data
    assert "windows_path" in data
    assert data["windows_path"].startswith("D:\\AI\\Cache\\uploads\\")
    uploaded_path = Path(data["file_path"])
    assert uploaded_path.exists()
    assert uploaded_path.read_bytes() == file_bytes
    # Clean up test upload
    uploaded_path.unlink()

def test_media_stream_security_and_streaming():
    # Attempting to stream unauthorized system file must return 403
    response = client.get("/api/v1/media/stream?path=/etc/passwd")
    assert response.status_code == 403

    # Attempting to stream non-existent file in allowed directory returns 404
    response = client.get("/api/v1/media/stream?path=/mnt/d/Output/Videos/non_existent.mp4")
    assert response.status_code == 404

    # Streaming existing test media returns 200
    test_wav = "/mnt/d/AI/Cache/stt-test/meeting.wav"
    response = client.get(f"/api/v1/media/stream?path={test_wav}")
    assert response.status_code == 200
    assert "audio" in response.headers.get("content-type", "")

def test_vault_note_endpoint():
    # Attempting path outside vault returns 403
    response = client.get("/api/v1/vault/note?path=/mnt/d/Output/Videos/test.mp4")
    assert response.status_code == 403

    # Attempting non-markdown file inside vault (if any) or non-existent note returns 404
    response = client.get("/api/v1/vault/note?path=/mnt/d/AI/Vault/Transcripts/non_existent_note.md")
    assert response.status_code == 404

    # Reading existing sample note
    sample_note = "/mnt/d/AI/Vault/Transcripts/sample_transcription_format.md"
    response = client.get(f"/api/v1/vault/note?path={sample_note}")
    assert response.status_code == 200
    data = response.json()
    assert data["filename"] == "sample_transcription_format.md"
    assert "obsidian_uri" in data
    assert f"obsidian://open?vault={settings.vault_name}" in data["obsidian_uri"]
    assert "content" in data
    assert len(data["content"]) > 0

def test_idle_timeout_config_endpoint():
    response = client.post("/api/v1/config/idle-timeout", json={
        "timeout_seconds": 300,
        "auto_close": True
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["idle_timeout_seconds"] == 300
    assert data["auto_close"] is True

    # Check health reflects the updated timeout
    health_resp = client.get("/api/v1/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["idle_timeout_seconds"] == 300

    # Reset back to 60 for consistency
    client.post("/api/v1/config/idle-timeout", json={
        "timeout_seconds": 60,
        "auto_close": True
    })

def test_job_cancel_and_clear_endpoints():
    from app.queue.manager import job_manager, JobStatus

    # Create a dummy job
    job_id = job_manager.create_job("test_op", "test_source")
    job_manager.update_job(job_id, status=JobStatus.RUNNING)

    # Cancel the job
    cancel_resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "ok"

    job = job_manager.get_job(job_id)
    assert job.status == JobStatus.CANCELLED


    # Clear finished jobs
    clear_resp = client.post("/api/v1/jobs/clear")
    assert clear_resp.status_code == 200
    assert clear_resp.json()["status"] == "ok"
    assert clear_resp.json()["cleared_count"] >= 1

    # Verify cleared job is no longer present
    assert job_manager.get_job(job_id) is None

def test_media_probe_endpoint():
    # Test URL probe with mock
    fake_probe_data = {
        "title": "Quantum Computing in 10 Minutes",
        "uploader": "Tech Explorer",
        "duration": 600.0,
        "duration_formatted": "10:00",
        "thumbnail": "https://example.com/thumb.jpg",
        "description": "A quick overview of qubits and superposition."
    }
    with mock.patch("app.routers.media.YtDlpService.probe_url", return_value=fake_probe_data):
        response = client.get("/api/v1/media/probe?url=https://www.youtube.com/watch?v=sample123")
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Quantum Computing in 10 Minutes"
        assert data["uploader"] == "Tech Explorer"
        assert data["duration_formatted"] == "10:00"
        assert data["thumbnail"] == "https://example.com/thumb.jpg"

    # Test local media probe
    local_file = "/mnt/d/AI/Cache/stt-test/meeting.wav"
    response = client.get(f"/api/v1/media/probe?url={local_file}")
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "meeting"
    assert data["duration"] > 0

def test_vault_recent_endpoint():
    response = client.get("/api/v1/vault/recent?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "notes" in data
    assert "total" in data
    assert isinstance(data["notes"], list)
    if len(data["notes"]) > 0:
        first = data["notes"][0]
        assert "filename" in first
        assert "obsidian_uri" in first
        assert "obsidian_link" in first
        assert "title" in first

def test_clip_endpoint_with_normalizer_and_speech_opt():
    with mock.patch("app.routers.media.run_clip_task") as mock_task:
        response = client.post("/api/v1/clip", json={
            "source": "/mnt/d/AI/Cache/stt-test/meeting.wav",
            "start_time": "00:00:01",
            "end_time": "00:00:05",
            "format": "wav",
            "normalize_audio": True,
            "speech_optimized": True
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"



