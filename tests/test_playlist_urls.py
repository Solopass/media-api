"""
Regression cover for the playlist run that failed on 2026-09-19.

A playlist URL was pasted into single ingest; nothing detected it, so yt-dlp
expanded the collection, picked an entry that happened to be unavailable
(DUi7hmQSCnc) and failed the job at the download stage. The same playlist also
exposed a second problem: expansion invented titles like "Video DUi7hmQSCnc" for
private and deleted entries, presenting guaranteed failures as ingestible items.
"""

import pytest
from unittest import mock
from fastapi.testclient import TestClient

from app.main import app
from app.services.ytdlp_service import YtDlpService, is_collection_url, is_unavailable_entry

client = TestClient(app)

FAILING_URL = "https://youtube.com/playlist?list=PLTDNS5RRx3UHS9GKxfyMI9tM-LX4Kxl3a&si=We9KvpSWeTpU8IuX"


# --------------------------------------------------------------------------
# Collection detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    FAILING_URL,
    "https://youtube.com/playlist?list=PL1",
    "https://www.youtube.com/@SomeChannel",
    "https://www.youtube.com/channel/UCabc123",
    "https://www.youtube.com/c/SomeChannel",
    "https://www.youtube.com/user/SomeUser",
])
def test_collection_urls_are_detected(url):
    assert is_collection_url(url) is True


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc123",
    # A video that merely sits in a playlist is still one video: noplaylist keeps it so.
    "https://www.youtube.com/watch?v=abc123&list=PL123",
    "https://example.com/podcast.mp3",
    "/mnt/d/Output/Videos/local.mp4",
    "D:\\Output\\Videos\\local.mp4",
    "",
])
def test_single_item_sources_are_not_collections(url):
    assert is_collection_url(url) is False


# --------------------------------------------------------------------------
# The ingest guard
# --------------------------------------------------------------------------

def test_ingest_refuses_a_playlist_url():
    with mock.patch("app.routers.ingest.job_manager.submit_pipeline") as submit:
        res = client.post("/api/v1/ingest", json={"source": FAILING_URL})

    assert res.status_code == 400
    assert "playlist" in res.json()["detail"].lower()
    assert "batch" in res.json()["detail"].lower()
    submit.assert_not_called(), "no job should be queued for a collection URL"


def test_ingest_still_accepts_a_single_video():
    with mock.patch("app.routers.ingest.job_manager.submit_pipeline"), \
         mock.patch("app.routers.ingest.job_manager.create_job", return_value="job-1"):
        res = client.post("/api/v1/ingest", json={"source": "https://www.youtube.com/watch?v=abc123"})

    assert res.status_code == 200


# --------------------------------------------------------------------------
# Unavailable entries
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    # Exactly what yt-dlp returned for the three dead entries in that playlist.
    {"id": "DUi7hmQSCnc", "title": None, "duration": None, "channel": None},
    {"id": "x", "title": "", "duration": None},
    {"id": "x", "title": "[Private video]", "duration": None},
    {"id": "x", "title": "[Deleted video]", "duration": None},
])
def test_unavailable_entries_are_recognised(entry):
    assert is_unavailable_entry(entry) is True


@pytest.mark.parametrize("entry", [
    {"id": "vuwOgKYwprQ", "title": "THE FOURTH DIMENSION - FULL AudioBook", "duration": 32707},
    # A live stream has no duration but is perfectly ingestible.
    {"id": "live1", "title": "Live right now", "duration": None},
])
def test_available_entries_are_kept(entry):
    assert is_unavailable_entry(entry) is False


def test_expansion_drops_unavailable_entries_and_counts_them():
    mock_ydl = mock.MagicMock()
    mock_ydl.extract_info.return_value = {
        "title": "Books Dolores Cannon",
        "uploader": "Someone",
        "entries": [
            {"id": "DUi7hmQSCnc", "title": None, "duration": None},
            {"id": "FnKSxiiGyFs", "title": None, "duration": None},
            {"id": "vuwOgKYwprQ", "title": "THE FOURTH DIMENSION", "duration": 32707},
            {"id": "cIQrrV7TBIE", "title": "The Convoluted Universe", "duration": 3514},
        ],
    }

    with mock.patch("yt_dlp.YoutubeDL") as ydl_cls:
        ydl_cls.return_value.__enter__.return_value = mock_ydl
        result = YtDlpService.extract_playlist(FAILING_URL, max_items=50)

    assert result["total_items"] == 2
    assert result["skipped_unavailable"] == 2
    titles = [i["title"] for i in result["items"]]
    assert titles == ["THE FOURTH DIMENSION", "The Convoluted Universe"]
    assert not any("Video DUi7hmQSCnc" in str(t) for t in titles), "dead entries must not get invented titles"


def test_expand_endpoint_reports_skipped_count():
    payload = {
        "title": "Books Dolores Cannon",
        "uploader": "Someone",
        "total_items": 1,
        "skipped_unavailable": 10,
        "items": [{
            "title": "Real Video", "url": "https://www.youtube.com/watch?v=vuwOgKYwprQ",
            "duration": 32707.0, "duration_formatted": "545:07", "thumbnail": None, "uploader": "X",
        }],
    }
    with mock.patch.object(YtDlpService, "extract_playlist", return_value=payload):
        res = client.post("/api/v1/batch/expand-playlist", json={"url": FAILING_URL})

    assert res.status_code == 200
    assert res.json()["skipped_unavailable"] == 10
    assert res.json()["total_items"] == 1
