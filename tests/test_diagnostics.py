import logging

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.logging_setup import log_file_path, recent_events, setup_logging
from app.main import app

client = TestClient(app)


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    return tmp_path


def test_client_log_records_an_error():
    res = client.post("/api/v1/client-log", json={
        "level": "error",
        "message": "Failed to fetch during batch ingest",
        "context": "batch-url-ingest",
        "url": "https://www.youtube.com/watch?v=abc",
        "stack": "TypeError: Failed to fetch\\n  at handleBatchUrls",
    })

    assert res.status_code == 200
    assert res.json()["recorded"] is True

    events = recent_events(limit=10, level="ERROR")
    assert any("Failed to fetch during batch ingest" in e["message"] for e in events)
    recorded = next(e for e in events if "Failed to fetch during batch ingest" in e["message"])
    assert "batch-url-ingest" in recorded["message"]
    assert "youtube.com" in recorded["message"]


def test_client_log_requires_a_message():
    res = client.post("/api/v1/client-log", json={"level": "error"})
    assert res.status_code == 422


def test_info_level_is_not_surfaced_as_a_problem():
    client.post("/api/v1/client-log", json={"level": "info", "message": "just a note"})
    assert not any("just a note" in e["message"] for e in recent_events(limit=50, level="WARNING"))


def test_diagnostics_reports_events_and_log_location():
    client.post("/api/v1/client-log", json={
        "level": "error", "message": "diagnostics probe failure", "context": "test"
    })

    res = client.get("/api/v1/diagnostics?limit=20&level=ERROR")

    assert res.status_code == 200
    data = res.json()
    assert data["log_file"].endswith("media-api.log")
    assert any("diagnostics probe failure" in e["message"] for e in data["events"])
    assert data["tail"] == [], "tail is opt-in"


def test_diagnostics_returns_newest_first():
    for i in range(3):
        client.post("/api/v1/client-log", json={
            "level": "error", "message": f"ordered probe {i}", "context": "test"
        })

    events = client.get("/api/v1/diagnostics?level=ERROR").json()["events"]
    probes = [e["message"] for e in events if "ordered probe" in e["message"]]
    assert "ordered probe 2" in probes[0]


@pytest.mark.parametrize("bad", ["?limit=0", "?limit=999", "?tail_lines=501"])
def test_diagnostics_bounds(bad):
    assert client.get(f"/api/v1/diagnostics{bad}").status_code == 422


def test_setup_logging_writes_a_file(log_env):
    path = setup_logging()

    assert path == log_file_path()
    logging.getLogger("media_api.test").error("file handler probe")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert path.exists()
    assert "file handler probe" in path.read_text(encoding="utf-8")


def test_setup_logging_survives_an_unwritable_directory(monkeypatch, tmp_path):
    """Logging must never be the reason the server fails to start."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(settings, "cache_dir", blocker)

    setup_logging()  # must not raise

    assert recent_events(limit=5) is not None


def test_unhandled_exceptions_are_logged(monkeypatch):
    from app.routers import diagnostics as diagnostics_router

    def boom(*args, **kwargs):
        raise RuntimeError("exploded inside a route")

    monkeypatch.setattr(diagnostics_router, "recent_events", boom)

    with pytest.raises(RuntimeError):
        # TestClient re-raises; the handler still logs on the way out.
        client.get("/api/v1/diagnostics")

    assert any(
        "exploded inside a route" in e["message"] or "unhandled" in e["message"].lower()
        for e in recent_events(limit=20, level="ERROR")
    )
