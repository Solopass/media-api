import pytest
import unittest.mock as mock
from fastapi.testclient import TestClient

from app.main import app
from app.services import stt_service
from app.services.parakeet_service import parse_tokens, tokens_to_segments

client = TestClient(app)

# Trimmed real parakeet-cli -ps stderr (CRLF line endings, as the Windows binary prints them)
STDERR = "\r\n".join([
    "read_audio_data: reading audio data from 'D:/x.wav' ...",
    'Segment 0: [0 -> 4910] "Hi there. Bye."',
    "Tokens [5]:",
    '  [ 0] id=  499 frame=  0 dur_idx= 2 dur_val= 2 p=1.0000 plog=-0.0000 t0=   0 t1=  16 word_start=true "\u2581Hi"',
    '  [ 1] id=  506 frame=  8 dur_idx= 2 dur_val= 2 p=0.9999 plog=-0.0001 t0=  64 t1=  80 word_start=true "\u2581the"',
    '  [ 2] id=  292 frame=  9 dur_idx= 2 dur_val= 2 p=1.0000 plog=-0.0000 t0=  80 t1=  96 word_start=false "re"',
    '  [ 3] id= 7883 frame= 10 dur_idx= 1 dur_val= 1 p=0.9072 plog=-0.0974 t0=  96 t1=  96 word_start=false "."',
    '  [ 4] id=  333 frame=400 dur_idx= 2 dur_val= 2 p=1.0000 plog=-0.0000 t0=3200 t1=3216 word_start=true "\u2581Bye."',
])


def test_parse_tokens_reads_centiseconds():
    tokens = parse_tokens(STDERR)
    assert len(tokens) == 5
    assert tokens[0] == {"t0": 0.0, "t1": 0.16, "text": "\u2581Hi"}
    assert tokens[4]["t0"] == 32.0


def test_tokens_group_into_sentences():
    segments = tokens_to_segments(parse_tokens(STDERR))
    assert [s["text"] for s in segments] == ["Hi there.", "Bye."]
    assert segments[0]["start"] == 0.0 and segments[0]["end"] == 0.96
    assert segments[1]["start"] == 32.0


def test_long_run_without_punctuation_is_split():
    tokens = [{"t0": i * 1.0, "t1": i * 1.0 + 0.5, "text": "\u2581word"} for i in range(70)]
    segments = tokens_to_segments(tokens)
    assert len(segments) == 3
    assert all(s["end"] - s["start"] <= 31 for s in segments)


def test_engine_defaults_to_parakeet():
    assert stt_service.resolve_engine(None, None) == "parakeet"
    assert stt_service.resolve_engine(None, "de") == "parakeet"


def test_unsupported_language_falls_back_to_whisper():
    assert stt_service.resolve_engine("parakeet", "zh") == "whisper"
    assert stt_service.resolve_engine(None, "ja") == "whisper"


def test_unknown_engine_is_rejected():
    with pytest.raises(ValueError):
        stt_service.resolve_engine("vosk", None)
    response = client.post("/api/v1/transcribe", json={"source": "/mnt/d/x.wav", "engine": "vosk"})
    assert response.status_code == 422


def test_transcribe_endpoint_reports_engine():
    with mock.patch("app.routers.transcribe.run_transcribe_task"):
        response = client.post("/api/v1/transcribe", json={"source": "/mnt/d/x.wav"})
    assert response.status_code == 200
    assert "parakeet" in response.json()["message"]


def test_run_transcribe_task_generates_transcript_json(tmp_path):
    import json
    from pathlib import Path
    from app.routers.transcribe import run_transcribe_task
    from app.models.schemas import TranscribeRequest
    from app.queue.manager import job_manager

    fake_audio = tmp_path / "sample.wav"
    fake_audio.write_bytes(b"RIFFdummywavdata")

    fake_res = {
        "engine": "parakeet",
        "model": "parakeet-tdt-0.6b-v3-q8_0",
        "language": "en",
        "duration": 5.2,
        "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "Hello world"}],
        "full_text": "Hello world",
    }

    job_id = job_manager.create_job("transcribe", str(fake_audio))
    req = TranscribeRequest(source=str(fake_audio), engine="parakeet")

    with mock.patch("app.routers.transcribe.stt_service.transcribe", return_value=fake_res):
        run_transcribe_task(job_id, req)

    job = job_manager.get_job(job_id)
    assert job.status == "completed"
    assert "transcript_json" in job.output_files
    assert "text" in job.output_files

    json_file = Path(job.output_files["transcript_json"])
    assert json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["full_text"] == "Hello world"
    assert data["media"]["title"] == "sample"
    assert len(data["segments"]) == 1
    assert data["segments"][0]["text"] == "Hello world"

    txt_file = Path(job.output_files["text"])
    assert txt_file.exists()
    assert txt_file.read_text(encoding="utf-8") == "Hello world"

