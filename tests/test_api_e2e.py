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


