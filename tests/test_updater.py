import time
import pytest
from unittest import mock
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.updater_service import UpdaterService

client = TestClient(app)

def test_updater_get_version():
    ver = UpdaterService.get_installed_version()
    assert isinstance(ver, str)
    assert len(ver) > 0

def test_updater_should_update_logic(tmp_path):
    mock_ts_file = tmp_path / "test_update_ts.txt"
    with mock.patch.object(UpdaterService, "get_timestamp_file", return_value=mock_ts_file):
        # 1. No file -> should update
        assert UpdaterService.should_update() is True

        # 2. Recent timestamp -> should skip
        mock_ts_file.write_text(str(time.time()), encoding="utf-8")
        assert UpdaterService.should_update() is False

        # 3. Old timestamp (> 24 hours) -> should update
        old_time = time.time() - (settings.ytdlp_update_interval_seconds + 100)
        mock_ts_file.write_text(str(old_time), encoding="utf-8")
        assert UpdaterService.should_update() is True

import asyncio

def test_updater_run_update_skipped(tmp_path):
    mock_ts_file = tmp_path / "test_update_ts.txt"
    mock_ts_file.write_text(str(time.time()), encoding="utf-8")
    with mock.patch.object(UpdaterService, "get_timestamp_file", return_value=mock_ts_file):
        res = asyncio.run(UpdaterService.run_update(force=False))
        assert res["status"] == "skipped"
        assert "Checked recently" in res["message"]

def test_update_ytdlp_api_endpoint():
    with mock.patch.object(UpdaterService, "run_update") as mock_run:
        mock_run.return_value = {
            "status": "success",
            "version": "2026.08.19",
            "previous_version": "2026.08.19",
            "updated": False,
            "output": "All requirements satisfied"
        }
        res = client.post("/api/v1/update-ytdlp?force=true")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["version"] == "2026.08.19"
