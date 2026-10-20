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


def _fake_proc(returncode, stderr=b""):
    proc = mock.MagicMock()
    proc.returncode = returncode
    proc.communicate = mock.AsyncMock(return_value=(b"", stderr))
    return proc


def test_run_update_records_timestamp_only_on_success(tmp_path):
    ts_file = tmp_path / "ts.txt"
    with mock.patch.object(UpdaterService, "get_timestamp_file", return_value=ts_file), \
         mock.patch("asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=_fake_proc(1, b"boom"))):
        res = asyncio.run(UpdaterService.run_update(force=True))
    assert res["status"] == "error"
    assert not ts_file.exists()

    with mock.patch.object(UpdaterService, "get_timestamp_file", return_value=ts_file), \
         mock.patch("asyncio.create_subprocess_exec", new=mock.AsyncMock(return_value=_fake_proc(0))):
        res = asyncio.run(UpdaterService.run_update(force=True))
    assert res["status"] == "success"
    assert ts_file.exists()


def test_download_task_does_not_trigger_update():
    from app.routers.media import run_download_task
    from app.models.schemas import DownloadRequest
    from app.queue.manager import job_manager

    job_id = job_manager.create_job("download", "https://example.com/v")
    req = DownloadRequest(url="https://example.com/v")
    with mock.patch.object(UpdaterService, "run_update") as upd, \
         mock.patch("app.routers.media.YtDlpService.download", side_effect=RuntimeError("stop")):
        run_download_task(job_id, req)
    upd.assert_not_called()


def test_update_command_upgrades_ytdlp_ejs_together():
    proc = _fake_proc(0)
    spawn = mock.AsyncMock(return_value=proc)
    with mock.patch("asyncio.create_subprocess_exec", new=spawn), \
         mock.patch.object(UpdaterService, "record_update_timestamp"):
        asyncio.run(UpdaterService.run_update(force=True))
    cmd = spawn.await_args_list[0].args
    assert "yt-dlp" in cmd and "yt-dlp-ejs" in cmd


def test_js_runtime_uses_node_without_remote_components():
    from app.services.ytdlp_service import _configure_js_runtime

    opts = {}
    with mock.patch("shutil.which", return_value="/usr/bin/node"):
        _configure_js_runtime(opts)
    assert opts["js_runtimes"] == {"node": {}}
    assert "remote_components" not in opts

    opts = {}
    with mock.patch("shutil.which", return_value=None):
        _configure_js_runtime(opts)
    assert opts == {}
