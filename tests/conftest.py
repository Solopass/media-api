"""
Shared test isolation.

Several tests drive the real endpoints, which reach the process-wide job manager
singleton. Left alone, they write test jobs into D:\\AI\\Cache\\media_jobs.json,
and test_job_cancel_and_clear_endpoints' call to /api/v1/jobs/clear deletes the
workstation's actual job history. The fixtures below keep the suite off the live
job store and off the real vault re-index.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_job_store(tmp_path_factory):
    """
    Point the job manager at a throwaway store for the whole session.

    The in-memory jobs loaded from the real file at import are dropped, so
    /api/v1/jobs/clear has nothing of the user's to purge, and nothing the suite
    creates is ever persisted to the real store.
    """
    from app.queue.manager import job_manager

    real_storage = job_manager._storage_file
    job_manager._storage_file = tmp_path_factory.mktemp("jobs") / "media_jobs.json"
    job_manager.jobs.clear()
    try:
        yield
    finally:
        job_manager._storage_file = real_storage
        job_manager.jobs.clear()


@pytest.fixture(scope="session", autouse=True)
def isolate_log_file(tmp_path_factory):
    """
    Keep the suite out of D:\AI\Cache\logs\media-api.log.

    That file is the record used to diagnose real failures, so test output
    landing in it is worse than noise: it makes a genuine incident harder to
    read afterwards.
    """
    from app.config import settings
    from app.logging_setup import setup_logging

    real_cache = settings.cache_dir
    settings.cache_dir = tmp_path_factory.mktemp("logs-root")
    setup_logging()
    try:
        yield
    finally:
        settings.cache_dir = real_cache
        setup_logging()


@pytest.fixture(scope="session", autouse=True)
def isolate_note_paths(tmp_path_factory):
    """
    Keep the suite out of the real vault and output folders.

    Tests that drive a failing ingest (source https://example.com/v) write a
    "FAILED: Unknown Media" error note, its mirror and a dashboard update. Left
    alone they land in D:\\AI\\Vault\\Transcripts, D:\\Output\\Transcripts and
    Transcripts_Dashboard.md (found 2026-11-17: four fake notes in the vault from
    test runs, two of them from the nightly fix lane's baseline run).
    """
    from app.config import settings

    root = tmp_path_factory.mktemp("notes")
    real = (settings.vault_transcripts, settings.vault_dashboard, settings.output_transcripts)
    settings.vault_transcripts = root / "Vault" / "Transcripts"
    settings.vault_dashboard = root / "Vault" / "Transcripts_Dashboard.md"
    settings.output_transcripts = root / "Output" / "Transcripts"
    for d in (settings.vault_transcripts, settings.output_transcripts):
        d.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        settings.vault_transcripts, settings.vault_dashboard, settings.output_transcripts = real


@pytest.fixture(autouse=True)
def no_vault_reindex(monkeypatch):
    """Never shell out to find-concept --reindex from a test."""
    monkeypatch.setattr("app.routers.ingest._trigger_reindex", lambda: None)
