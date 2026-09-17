import pytest
from fastapi.testclient import TestClient
from app.main import app

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
    assert data["default"] == "sol-fast"

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
    assert "obsidian://open?vault=Vault" in data["obsidian_uri"]
    assert "content" in data
    assert len(data["content"]) > 0


