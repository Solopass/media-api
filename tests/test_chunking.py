"""
Chunked transcription for long audio.

Regression cover for the 9h05m audiobook that could not be transcribed at all:
parakeet-cli exits 5 while decoding anything that long, at any sample rate, so
the recording has to be cut up before it reaches the engine.
"""

import subprocess
import time
from pathlib import Path

import pytest
from unittest import mock

from app.config import settings
from app.services import chunk_service, stt_service
from app.services.chunk_service import AudioChunk
from app.services.errors import TranscriptionCancelled

NINE_HOURS = 32706.0


# --------------------------------------------------------------------------
# When to chunk
# --------------------------------------------------------------------------

@pytest.mark.parametrize("duration,expected", [
    (60.0, False),
    (chunk_service.CHUNK_TARGET_SECONDS, False),
    (chunk_service.CHUNK_TARGET_SECONDS + 1, True),
    (NINE_HOURS, True),
])
def test_needs_chunking(duration, expected):
    assert chunk_service.needs_chunking(duration) is expected


# --------------------------------------------------------------------------
# Cut planning
# --------------------------------------------------------------------------

def test_short_audio_is_a_single_chunk():
    assert chunk_service.plan_cuts(600.0, silences=[]) == [0.0, 600.0]


def test_cuts_land_on_targets_without_silence():
    boundaries = chunk_service.plan_cuts(NINE_HOURS, silences=[])

    assert boundaries[0] == 0.0
    assert boundaries[-1] == NINE_HOURS
    interior = boundaries[1:-1]
    assert interior == [chunk_service.CHUNK_TARGET_SECONDS * i for i in range(1, len(interior) + 1)]


def test_cuts_snap_to_nearby_silence():
    """A cut moves to silence so a sentence is not sliced mid-word."""
    target = chunk_service.CHUNK_TARGET_SECONDS
    boundaries = chunk_service.plan_cuts(target * 2.5, silences=[target - 7.0, target + 300])

    assert boundaries[1] == target - 7.0


def test_distant_silence_is_ignored():
    target = chunk_service.CHUNK_TARGET_SECONDS
    far = target + chunk_service.SILENCE_SNAP_WINDOW + 30
    boundaries = chunk_service.plan_cuts(target * 2.5, silences=[far])

    assert boundaries[1] == target


def test_the_nearest_silence_wins():
    target = chunk_service.CHUNK_TARGET_SECONDS
    boundaries = chunk_service.plan_cuts(target * 2.5, silences=[target - 15, target - 2, target + 12])

    assert boundaries[1] == target - 2


def test_no_sliver_chunk_at_the_end():
    """A few seconds left over belongs on the previous chunk, not its own."""
    duration = chunk_service.CHUNK_TARGET_SECONDS * 2 + 5
    boundaries = chunk_service.plan_cuts(duration, silences=[])

    for index in range(len(boundaries) - 1):
        assert boundaries[index + 1] - boundaries[index] >= chunk_service.MIN_CHUNK_SECONDS


def test_boundaries_are_strictly_increasing():
    silences = [i * 37.5 for i in range(1, 400)]
    boundaries = chunk_service.plan_cuts(NINE_HOURS, silences=silences)

    assert boundaries == sorted(boundaries)
    assert len(set(boundaries)) == len(boundaries)
    assert len(boundaries) - 1 >= 20, "a nine-hour book should split into many chunks"


def test_silence_parsing_from_ffmpeg_output():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 118.5\n"
        "[silencedetect @ 0x1] silence_end: 119.7 | silence_duration: 1.2\n"
        "[silencedetect @ 0x1] silence_start: 240.0\n"
        "[silencedetect @ 0x1] silence_end: 241.0 | silence_duration: 1.0\n"
    )
    with mock.patch("subprocess.run", return_value=mock.Mock(stderr=stderr, returncode=0)):
        midpoints = chunk_service.detect_silences(Path("/tmp/a.wav"))

    assert midpoints == [pytest.approx(119.1), pytest.approx(240.5)]


def test_silence_detection_failure_is_not_fatal():
    """Without candidates the cuts simply land on their targets."""
    with mock.patch("subprocess.run", side_effect=OSError("ffmpeg missing")):
        assert chunk_service.detect_silences(Path("/tmp/a.wav")) == []


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def _chunk(index, start, duration):
    return AudioChunk(index=index, start=start, duration=duration, path=Path(f"/tmp/c{index}.wav"))


def test_merge_offsets_timestamps_by_chunk_start():
    chunks = [_chunk(0, 0.0, 1200.0), _chunk(1, 1200.0, 1200.0)]
    results = [
        {"engine": "parakeet", "model": "m", "language": "en",
         "segments": [{"id": 0, "start": 0.0, "end": 5.0, "text": "first"}],
         "full_text": "first"},
        {"engine": "parakeet", "model": "m", "language": "en",
         "segments": [{"id": 0, "start": 3.0, "end": 8.0, "text": "second"}],
         "full_text": "second"},
    ]

    merged = stt_service.merge_chunk_results(results, chunks, 2400.0)

    assert [s["start"] for s in merged["segments"]] == [0.0, 1203.0]
    assert [s["end"] for s in merged["segments"]] == [5.0, 1208.0]
    assert merged["full_text"] == "first second"
    assert merged["duration"] == 2400.0
    assert merged["chunks"] == 2


def test_merged_segment_ids_are_sequential():
    chunks = [_chunk(0, 0.0, 1200.0), _chunk(1, 1200.0, 1200.0)]
    results = [
        {"segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "a"},
                      {"id": 1, "start": 1.0, "end": 2.0, "text": "b"}], "full_text": "a b"},
        {"segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "c"}], "full_text": "c"},
    ]

    merged = stt_service.merge_chunk_results(results, chunks, 2400.0)

    assert [s["id"] for s in merged["segments"]] == [0, 1, 2]


def test_merged_timestamps_never_go_backwards():
    chunks = [_chunk(i, i * 1200.0, 1200.0) for i in range(5)]
    results = [
        {"segments": [{"id": 0, "start": 10.0, "end": 900.0, "text": f"chunk {i}"}],
         "full_text": f"chunk {i}"}
        for i in range(5)
    ]

    merged = stt_service.merge_chunk_results(results, chunks, 6000.0)
    starts = [s["start"] for s in merged["segments"]]

    assert starts == sorted(starts)


def test_merge_tolerates_an_empty_chunk():
    """A silent stretch transcribes to nothing; it must not break the join."""
    chunks = [_chunk(0, 0.0, 1200.0), _chunk(1, 1200.0, 1200.0)]
    results = [
        {"segments": [{"id": 0, "start": 0.0, "end": 5.0, "text": "hello"}], "full_text": "hello"},
        {"segments": [], "full_text": ""},
    ]

    merged = stt_service.merge_chunk_results(results, chunks, 2400.0)

    assert merged["full_text"] == "hello"
    assert len(merged["segments"]) == 1


# --------------------------------------------------------------------------
# The chunk loop
# --------------------------------------------------------------------------

@pytest.fixture
def chunk_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)

    chunks = [_chunk(0, 0.0, 1200.0), _chunk(1, 1200.0, 1200.0), _chunk(2, 2400.0, 600.0)]
    monkeypatch.setattr(chunk_service, "split", lambda *a, **k: chunks)
    monkeypatch.setattr(stt_service.chunk_service, "split", lambda *a, **k: chunks)
    return chunks


def test_long_audio_is_transcribed_chunk_by_chunk(chunk_env, monkeypatch):
    calls = []

    def fake_transcribe(path, language=None, should_cancel=None):
        calls.append(path.name)
        return {"engine": "parakeet", "model": "m", "language": "en",
                "segments": [{"id": 0, "start": 1.0, "end": 2.0, "text": f"text {len(calls)}"}],
                "full_text": f"text {len(calls)}"}

    monkeypatch.setattr(stt_service.ParakeetService, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 3000.0)

    result = stt_service.transcribe(Path("/tmp/long.mp3"), engine="parakeet")

    assert len(calls) == 3, "every chunk must be transcribed"
    assert result["chunks"] == 3
    assert result["full_text"] == "text 1 text 2 text 3"


def test_progress_is_monotonic_and_completes(chunk_env, monkeypatch):
    monkeypatch.setattr(
        stt_service.ParakeetService, "transcribe",
        lambda path, language=None, should_cancel=None: {
            "segments": [], "full_text": "", "engine": "parakeet", "model": "m"},
    )
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 3000.0)
    fractions = []

    stt_service.transcribe(
        Path("/tmp/long.mp3"), engine="parakeet",
        progress_callback=lambda fraction, position, estimated=False: fractions.append(fraction),
    )

    assert fractions == sorted(fractions), "progress must never go backwards"
    assert fractions[-1] == pytest.approx(1.0), "the last chunk means done"
    assert len([f for f in fractions if f < 1.0]) >= 2, "progress must step per chunk"


def test_cancelling_stops_between_chunks(chunk_env, monkeypatch):
    calls = []

    def fake_transcribe(path, language=None, should_cancel=None):
        calls.append(path.name)
        return {"segments": [], "full_text": "", "engine": "parakeet", "model": "m"}

    monkeypatch.setattr(stt_service.ParakeetService, "transcribe", fake_transcribe)
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 3000.0)

    with pytest.raises(TranscriptionCancelled):
        stt_service.transcribe(
            Path("/tmp/long.mp3"), engine="parakeet",
            should_cancel=lambda: len(calls) >= 1,
        )

    assert len(calls) == 1, "cancellation must not start another chunk"


def test_cancellation_does_not_trigger_the_whisper_fallback(chunk_env, monkeypatch):
    """Falling back here would restart on whisper the work the user just stopped."""
    monkeypatch.setattr(
        stt_service.ParakeetService, "transcribe",
        mock.Mock(side_effect=TranscriptionCancelled("stopped")),
    )
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 3000.0)
    whisper = mock.Mock()
    monkeypatch.setattr(stt_service.WhisperService, "transcribe", whisper)

    with pytest.raises(TranscriptionCancelled):
        stt_service.transcribe(Path("/tmp/long.mp3"), engine="parakeet")

    whisper.assert_not_called()


def test_a_real_failure_still_falls_back_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 600.0)
    monkeypatch.setattr(
        stt_service.ParakeetService, "transcribe",
        mock.Mock(side_effect=RuntimeError("exit 5")),
    )
    monkeypatch.setattr(
        stt_service.WhisperService, "transcribe",
        lambda *a, **k: {"segments": [], "full_text": "whisper text", "duration": 600.0},
    )
    notices = []

    result = stt_service.transcribe(
        Path("/tmp/short.mp3"), engine="parakeet", status_callback=notices.append
    )

    assert result["engine"] == "whisper (fallback)"
    assert notices and "falling back" in notices[0].lower(), "a silent fallback is what hid this bug"


def test_short_audio_skips_chunking_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    monkeypatch.setattr(stt_service, "_audio_duration", lambda p: 120.0)
    split = mock.Mock()
    monkeypatch.setattr(stt_service.chunk_service, "split", split)
    monkeypatch.setattr(
        stt_service.ParakeetService, "transcribe",
        lambda path, language=None, should_cancel=None: {
            "segments": [], "full_text": "short", "engine": "parakeet", "model": "m", "duration": 120.0},
    )

    result = stt_service.transcribe(Path("/tmp/short.mp3"), engine="parakeet")

    split.assert_not_called()
    assert result["full_text"] == "short"
