import json
from pathlib import Path

import pytest
from unittest import mock
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import chapters_service
from app.services.chapters_service import (
    build_timestamped_digest, parse_chapters, seconds_to_timestamp,
    timestamp_to_seconds, to_youtube_block,
)
from app.services.obsidian_service import ObsidianService

client = TestClient(app)

SAMPLE_RESPONSE = """00:00 Cold Open
05:30 Architecture Overview
12:45 Trade-offs And Limits
21:10 Closing Thoughts
"""


def _segments(count=180, step=10.0):
    return [
        {"start": i * step, "end": i * step + step, "text": f"segment {i} discussing topic {i // 6}"}
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# Timestamp helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stamp,expected", [
    ("00:00", 0), ("03:45", 225), ("1:02:03", 3723), ("00:59", 59),
])
def test_timestamp_to_seconds(stamp, expected):
    assert timestamp_to_seconds(stamp) == expected


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00"), (225, "03:45"), (3723, "1:02:03"), (59.9, "00:59"),
])
def test_seconds_to_timestamp(seconds, expected):
    assert seconds_to_timestamp(seconds) == expected


def test_timestamp_to_seconds_rejects_nonsense():
    assert timestamp_to_seconds("later") is None
    assert timestamp_to_seconds("aa:bb") is None


# --------------------------------------------------------------------------
# Digest: the model must see the whole recording
# --------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [1200, 6000, 24000])
def test_digest_spans_the_full_duration(budget):
    segments = _segments()
    total = segments[-1]["end"]

    digest = build_timestamped_digest(segments, max_chars=budget)
    lines = digest.splitlines()

    assert len(digest) <= budget
    assert lines[0].startswith("[00:00]")
    # The last anchor must land near the end, not partway through: a truncated
    # digest would silently produce chapters only for the opening minutes.
    last_stamp = timestamp_to_seconds(lines[-1].split("]")[0].lstrip("["))
    assert last_stamp > total * 0.9


def test_digest_handles_short_and_empty_input():
    assert build_timestamped_digest([]) == ""
    assert build_timestamped_digest([{"start": 0.0, "end": 1.0, "text": "hello"}]) == "[00:00] hello"
    assert build_timestamped_digest([{"start": 0.0, "text": ""}]) == ""


def test_digest_skips_malformed_segments():
    segments = [
        {"start": 0.0, "end": 5.0, "text": "good"},
        {"text": "no start"},
        "not a dict",
        {"start": 10.0, "end": 15.0, "text": "also good"},
    ]
    digest = build_timestamped_digest(segments)
    assert "good" in digest
    assert "no start" not in digest


# --------------------------------------------------------------------------
# Parsing: nothing the model returns is trusted
# --------------------------------------------------------------------------

def test_parse_accepts_common_formats():
    raw = "00:00 Intro\n[05:30] - Middle\n12:45 — End Section\n* 18:00 Bulleted"
    chapters = parse_chapters(raw, duration=1800)
    assert [c["timestamp"] for c in chapters] == ["00:00", "05:30", "12:45", "18:00"]
    assert chapters[2]["title"] == "End Section"


def test_parse_drops_timestamps_past_the_duration():
    raw = "00:00 Intro\n05:00 Real Topic\n99:00 Invented Topic"
    chapters = parse_chapters(raw, duration=600)
    assert [c["timestamp"] for c in chapters] == ["00:00", "05:00"]


def test_parse_repairs_ordering_and_forces_a_zero_start():
    raw = "07:00 Later\n02:00 Earlier\n11:00 Latest"
    chapters = parse_chapters(raw, duration=1200)
    starts = [c["start"] for c in chapters]
    assert starts == sorted(starts)
    assert chapters[0]["start"] == 0.0, "a chapter list must begin at 00:00"


def test_parse_drops_duplicates_and_near_neighbours():
    raw = "00:00 Intro\n00:05 Intro\n00:10 Barely Later\n10:00 Real Topic"
    chapters = parse_chapters(raw, duration=1200)
    assert len(chapters) == 2
    assert chapters[1]["timestamp"] == "10:00"


def test_parse_caps_the_count():
    raw = "\n".join(f"{i:02d}:00 Chapter {i}" for i in range(40))
    chapters = parse_chapters(raw, duration=40 * 60)
    assert len(chapters) == chapters_service.MAX_CHAPTERS


def test_parse_rejects_thin_results():
    """One usable line is not a chapter list; no chapters beats wrong chapters."""
    assert parse_chapters("00:00 Only One", duration=600) == []
    assert parse_chapters("I could not find any chapters in this transcript.", duration=600) == []
    assert parse_chapters("", duration=600) == []


def test_parse_strips_markdown_from_titles():
    chapters = parse_chapters("00:00 **Bold Intro**\n05:00 `Code Talk`", duration=600)
    assert chapters[0]["title"] == "Bold Intro"
    assert chapters[1]["title"] == "Code Talk"


def test_youtube_block_format():
    chapters = parse_chapters(SAMPLE_RESPONSE, duration=1800)
    block = to_youtube_block(chapters)
    assert block.splitlines()[0] == "00:00 Cold Open"
    assert all(" " in line for line in block.splitlines())


# --------------------------------------------------------------------------
# generate_chapters
# --------------------------------------------------------------------------

def test_generate_chapters_uses_the_digest_and_validates():
    with mock.patch("app.services.chapters_service._ollama_chat", return_value=SAMPLE_RESPONSE) as chat:
        chapters = chapters_service.generate_chapters("Talk", _segments(), 1800.0)

    assert len(chapters) == 4
    prompt = chat.call_args[0][1]
    assert "[00:00]" in prompt, "the model must be shown timestamps"
    assert "Total duration: 30:00" in prompt


def test_generate_chapters_degrades_when_ollama_is_down():
    from app.services.obsidian_service import OllamaError

    with mock.patch("app.services.chapters_service._ollama_chat", side_effect=OllamaError("refused")):
        assert chapters_service.generate_chapters("Talk", _segments(), 1800.0) == []


def test_generate_chapters_without_segments():
    assert chapters_service.generate_chapters("Talk", [], 0.0) == []


# --------------------------------------------------------------------------
# Note rendering
# --------------------------------------------------------------------------

@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    for name in ("vault_transcripts", "output_transcripts", "output_audio", "output_videos"):
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, name, target)
    monkeypatch.setattr(settings, "vault_dashboard", tmp_path / "Dashboard.md")
    return tmp_path


def _note(chapters=None):
    return ObsidianService.create_transcript_note(
        media_info={"title": "Test Talk", "file_path": "/mnt/d/Output/Audio/test.mp3", "uploader": "X", "url": "u"},
        transcription={"segments": [{"start": 0.0, "end": 1.0, "text": "hello"}], "duration": 1800, "model": "stub"},
        summary="A summary.",
        chapters=chapters,
    )


def test_note_carries_both_chapter_forms(vault_env):
    chapters = parse_chapters(SAMPLE_RESPONSE, duration=1800)
    note = _note(chapters)
    content = note.read_text(encoding="utf-8")

    assert "chapters:\n" in content
    assert '  - "00:00 Cold Open"' in content
    assert "## 🎬 Chapters" in content
    assert "- `05:30` — **Architecture Overview**" in content


def test_note_without_chapters_is_unchanged(vault_env):
    content = _note(None).read_text(encoding="utf-8")
    assert "chapters:" not in content
    assert "🎬 Chapters" not in content


def test_update_note_chapters_inserts_then_replaces(vault_env):
    note = _note(None)
    first = parse_chapters(SAMPLE_RESPONSE, duration=1800)

    assert ObsidianService.update_note_chapters(note, first) is True
    content = note.read_text(encoding="utf-8")
    assert content.count("## 🎬 Chapters") == 1
    assert '  - "12:45 Trade-offs And Limits"' in content

    second = parse_chapters("00:00 Fresh Start\n09:00 Second Pass", duration=1800)
    assert ObsidianService.update_note_chapters(note, second) is True
    content = note.read_text(encoding="utf-8")

    assert content.count("## 🎬 Chapters") == 1, "a second run must replace, not duplicate"
    assert content.count("chapters:\n") == 1
    assert "Trade-offs And Limits" not in content
    assert "- `09:00` — **Second Pass**" in content
    # The rest of the note survives.
    assert "## 📋 Metadata" in content
    assert "A summary." in content


def test_update_note_summary_still_targets_only_the_callout(vault_env):
    """Regression: the Chapters section sits between the callout and Metadata."""
    note = _note(parse_chapters(SAMPLE_RESPONSE, duration=1800))

    ObsidianService.update_note_summary(note, "Replacement summary.", "executive")
    content = note.read_text(encoding="utf-8")

    assert "Replacement summary." in content
    assert "A summary." not in content
    assert "## 🎬 Chapters" in content, "chapters must survive a re-summarize"
    assert "- `05:30` — **Architecture Overview**" in content


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------

def _seed_note_with_sidecar(tmp_path):
    note = settings.vault_transcripts / "note.md"
    note.write_text(
        "---\ntitle: \"Seeded\"\ntags: [media]\n---\n\n# 🎙️ Seeded\n\n"
        "> [!abstract] **Executive Briefing**\n> Summary here.\n\n---\n\n"
        "## 📋 Metadata\n- **Source Media**: `/mnt/d/Output/Audio/seeded.mp3`\n",
        encoding="utf-8",
    )
    sidecar = settings.output_audio / "seeded.transcript.json"
    sidecar.write_text(json.dumps({"segments": _segments(), "duration": 1800}), encoding="utf-8")
    return note, sidecar


def test_chapters_endpoint_writes_note_and_sidecar(vault_env, tmp_path):
    note, sidecar = _seed_note_with_sidecar(tmp_path)

    with mock.patch("app.services.chapters_service._ollama_chat", return_value=SAMPLE_RESPONSE):
        res = client.post("/api/v1/vault/chapters", json={"note_path": str(note)})

    assert res.status_code == 200
    data = res.json()
    assert len(data["chapters"]) == 4
    assert data["youtube_block"].startswith("00:00 Cold Open")
    assert "## 🎬 Chapters" in note.read_text(encoding="utf-8")
    assert json.loads(sidecar.read_text(encoding="utf-8"))["chapters"][0]["title"] == "Cold Open"


def test_chapters_endpoint_falls_back_to_the_transcript_table(vault_env):
    """Notes whose sidecar was reclaimed still get chapters from their table."""
    note = settings.vault_transcripts / "table_only.md"
    rows = "\n".join(f"| `{i:02d}:00` | Topic {i} discussion |" for i in range(30))
    note.write_text(
        "---\ntitle: \"Table Only\"\n---\n\n# 🎙️ Table Only\n\n"
        "> [!abstract] **Executive Briefing**\n> Summary.\n\n---\n\n"
        "## 📝 Full Timestamped Transcript\n\n| Timestamp | Content |\n| :--- | :--- |\n" + rows,
        encoding="utf-8",
    )

    with mock.patch("app.services.chapters_service._ollama_chat", return_value=SAMPLE_RESPONSE):
        res = client.post("/api/v1/vault/chapters", json={"note_path": str(note)})

    assert res.status_code == 200
    assert "## 🎬 Chapters" in note.read_text(encoding="utf-8")


def test_chapters_endpoint_rejects_paths_outside_the_vault(vault_env, tmp_path):
    outsider = tmp_path / "outside.md"
    outsider.write_text("# nope", encoding="utf-8")

    res = client.post("/api/v1/vault/chapters", json={"note_path": str(outsider)})

    assert res.status_code == 403
    assert outsider.read_text(encoding="utf-8") == "# nope"


def test_chapters_endpoint_rejects_non_markdown(vault_env):
    target = settings.vault_transcripts / "notes.txt"
    target.write_text("plain", encoding="utf-8")

    res = client.post("/api/v1/vault/chapters", json={"note_path": str(target)})

    assert res.status_code == 400


def test_chapters_endpoint_404s_for_missing_note(vault_env):
    res = client.post("/api/v1/vault/chapters", json={"note_path": "no_such_note.md"})
    assert res.status_code == 404


def test_chapters_endpoint_reports_when_no_transcript_is_available(vault_env):
    note = settings.vault_transcripts / "bare.md"
    note.write_text("---\ntitle: \"Bare\"\n---\n\n# 🎙️ Bare\n\nNo transcript here.\n", encoding="utf-8")

    res = client.post("/api/v1/vault/chapters", json={"note_path": str(note)})

    assert res.status_code == 400
    assert "No timestamped transcript" in res.json()["detail"]


def test_chapters_endpoint_reports_unusable_generations(vault_env, tmp_path):
    note, _ = _seed_note_with_sidecar(tmp_path)

    with mock.patch("app.services.chapters_service._ollama_chat", return_value="I cannot help with that."):
        res = client.post("/api/v1/vault/chapters", json={"note_path": str(note)})

    assert res.status_code == 422
    assert "## 🎬 Chapters" not in note.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

def test_ingest_request_defaults_to_no_chapters():
    from app.models.schemas import IngestRequest
    assert IngestRequest(source="https://example.com/v").generate_chapters is False
