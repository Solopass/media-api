"""
Progress reporting for the long stages.

A nine-hour audiobook spends ~5 minutes in ffmpeg and ~50 in transcription. With
four coarse checkpoints, both looked identical to a hang. ffmpeg can stream its
position; parakeet-cli cannot (verified: every token line of a 4-minute file
arrives in one burst when the process exits), so it gets a calibrated estimate
that is labelled as one.
"""

import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.routers import ingest as ingest_mod
from app.services import stt_service


@pytest.fixture
def rate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# Learned throughput
# --------------------------------------------------------------------------

def test_default_rate_without_history(rate_env):
    assert stt_service.observed_realtime_factor() == stt_service.DEFAULT_REALTIME_FACTOR


def test_rate_is_averaged_over_samples(rate_env):
    (rate_env / "stt_rate.json").write_text(json.dumps({"samples": [8.0, 10.0, 12.0]}), encoding="utf-8")
    assert stt_service.observed_realtime_factor() == pytest.approx(10.0)


def test_recording_a_run_updates_the_estimate(rate_env):
    stt_service.record_realtime_factor(audio_seconds=240.0, wall_seconds=24.0)

    samples = json.loads((rate_env / "stt_rate.json").read_text(encoding="utf-8"))["samples"]
    assert samples == [10.0]
    assert stt_service.observed_realtime_factor() == pytest.approx(10.0)


def test_only_recent_samples_are_kept(rate_env):
    for i in range(stt_service.RATE_SAMPLES_KEPT + 5):
        stt_service.record_realtime_factor(audio_seconds=100.0 * (i + 1), wall_seconds=10.0)

    samples = json.loads((rate_env / "stt_rate.json").read_text(encoding="utf-8"))["samples"]
    assert len(samples) == stt_service.RATE_SAMPLES_KEPT


@pytest.mark.parametrize("audio,wall", [(0, 10), (100, 0), (-5, 10)])
def test_nonsense_measurements_are_ignored(rate_env, audio, wall):
    stt_service.record_realtime_factor(audio, wall)
    assert not (rate_env / "stt_rate.json").exists()


def test_unreadable_rate_file_falls_back_to_the_default(rate_env):
    (rate_env / "stt_rate.json").write_text("not json", encoding="utf-8")
    assert stt_service.observed_realtime_factor() == stt_service.DEFAULT_REALTIME_FACTOR


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------

def test_heartbeat_reports_estimates_while_a_silent_engine_runs(rate_env, monkeypatch):
    monkeypatch.setattr(stt_service, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    events = []

    with stt_service._Heartbeat(audio_seconds=100.0, callback=lambda *a: events.append(a)):
        time.sleep(0.3)

    assert events, "a silent engine must still produce a sign of life"
    assert all(estimated is True for _, _, estimated in events), "estimates must be labelled"
    assert all(0.0 <= fraction <= 0.99 for fraction, _, _ in events)


def test_heartbeat_never_reaches_completion(rate_env, monkeypatch):
    """Only the process exiting means done; the estimate must not claim it."""
    monkeypatch.setattr(stt_service, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    (rate_env / "stt_rate.json").write_text(json.dumps({"samples": [1000.0]}), encoding="utf-8")
    events = []

    with stt_service._Heartbeat(audio_seconds=1.0, callback=lambda *a: events.append(a)):
        time.sleep(0.3)

    assert max(fraction for fraction, _, _ in events) <= 0.99


def test_heartbeat_stops_when_the_stage_finishes(rate_env, monkeypatch):
    monkeypatch.setattr(stt_service, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    events = []

    with stt_service._Heartbeat(audio_seconds=100.0, callback=lambda *a: events.append(a)):
        time.sleep(0.2)
    settled = len(events)
    time.sleep(0.2)

    assert len(events) == settled, "the heartbeat thread must not outlive the stage"


def test_heartbeat_is_inert_without_a_duration(rate_env, monkeypatch):
    monkeypatch.setattr(stt_service, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    events = []

    with stt_service._Heartbeat(audio_seconds=0.0, callback=lambda *a: events.append(a)):
        time.sleep(0.1)

    assert events == [], "no duration means no honest estimate to give"


def test_a_failing_callback_cannot_break_transcription(rate_env, monkeypatch):
    monkeypatch.setattr(stt_service, "HEARTBEAT_INTERVAL_SECONDS", 0.02)

    def boom(*args):
        raise RuntimeError("UI went away")

    with stt_service._Heartbeat(audio_seconds=100.0, callback=boom):
        time.sleep(0.1)  # must not raise


# --------------------------------------------------------------------------
# Pipeline banding
# --------------------------------------------------------------------------

def test_stage_progress_maps_onto_its_band(monkeypatch):
    updates = []
    monkeypatch.setattr(ingest_mod.job_manager, "is_cancelled", lambda job_id: False)
    monkeypatch.setattr(ingest_mod.job_manager, "update_job", lambda job_id, **kw: updates.append(kw))

    report = ingest_mod._stage_progress("job-1", time.time(), 65.0, 84.0, "Transcribing")
    report(0.0, 0.0)
    report(0.5, 1800.0)
    report(1.0, 3600.0)

    assert [u["progress_percent"] for u in updates] == [65.0, 74.5, 84.0]


def test_stage_progress_marks_estimates(monkeypatch):
    updates = []
    monkeypatch.setattr(ingest_mod.job_manager, "is_cancelled", lambda job_id: False)
    monkeypatch.setattr(ingest_mod.job_manager, "update_job", lambda job_id, **kw: updates.append(kw))

    report = ingest_mod._stage_progress("job-1", time.time(), 65.0, 84.0, "Transcribing")
    report(0.5, 1800.0, True)
    report(0.5, 1800.0, False)

    assert "~" in updates[0]["speed"], "an extrapolated position must not read as measured"
    assert "~" not in updates[1]["speed"]


def test_stage_progress_stops_reporting_once_cancelled(monkeypatch):
    updates = []
    monkeypatch.setattr(ingest_mod.job_manager, "is_cancelled", lambda job_id: True)
    monkeypatch.setattr(ingest_mod.job_manager, "update_job", lambda job_id, **kw: updates.append(kw))

    ingest_mod._stage_progress("job-1", time.time(), 50.0, 62.0, "Extracting")(0.5, 100.0)

    assert updates == []


@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"), (59, "0:59"), (125, "2:05"), (3600, "1:00:00"), (32707, "9:05:07"),
])
def test_clock_formatting(seconds, expected):
    assert ingest_mod._format_clock(seconds) == expected


@pytest.mark.parametrize("seconds,expected", [
    (0, ""), (45, "45s"), (600, "10m"), (7200, "2.0h"),
])
def test_eta_formatting(seconds, expected):
    assert ingest_mod._format_eta(seconds) == expected


def test_eta_comes_only_from_measured_checkpoints(monkeypatch):
    """
    Extrapolating from elapsed time counted the minutes spent splitting a long
    recording as transcription, reporting "ETA 4.0h" for a 35-minute job.
    """
    updates = []
    monkeypatch.setattr(ingest_mod.job_manager, "is_cancelled", lambda job_id: False)
    monkeypatch.setattr(ingest_mod.job_manager, "update_job", lambda job_id, **kw: updates.append(kw))

    # Stage started 10 minutes ago; most of that was splitting, not transcribing.
    report = ingest_mod._stage_progress("job-1", time.time() - 600, 65.0, 84.0, "Transcribing")

    report(0.02, 13.0, True)
    assert "ETA" not in updates[-1]["speed"], "an in-chunk estimate cannot anchor an ETA"

    report(1 / 28, 1200.0, False)
    assert "ETA" not in updates[-1]["speed"], "one checkpoint gives no rate yet"

    time.sleep(0.05)
    report(2 / 28, 2400.0, False)
    assert "ETA" in updates[-1]["speed"], "two checkpoints give a real rate"
