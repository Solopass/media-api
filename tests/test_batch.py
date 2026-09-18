import pytest
import unittest.mock as mock
from fastapi.testclient import TestClient
from app.main import app
from app.services.ytdlp_service import YtDlpService

client = TestClient(app)

def test_expand_playlist_endpoint_success():
    mock_playlist_data = {
        "title": "Test Playlist",
        "uploader": "Test Creator",
        "total_items": 2,
        "items": [
            {
                "title": "Video 1",
                "url": "https://www.youtube.com/watch?v=vid1",
                "duration": 125.0,
                "duration_formatted": "02:05",
                "thumbnail": "https://img.youtube.com/vi/vid1/default.jpg",
                "uploader": "Test Creator"
            },
            {
                "title": "Video 2",
                "url": "https://www.youtube.com/watch?v=vid2",
                "duration": 300.0,
                "duration_formatted": "05:00",
                "thumbnail": "https://img.youtube.com/vi/vid2/default.jpg",
                "uploader": "Test Creator"
            }
        ]
    }

    with mock.patch.object(YtDlpService, "extract_playlist", return_value=mock_playlist_data):
        response = client.post("/api/v1/batch/expand-playlist", json={
            "url": "https://www.youtube.com/playlist?list=PL12345",
            "max_items": 10
        })
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Playlist"
        assert data["uploader"] == "Test Creator"
        assert data["total_items"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["url"] == "https://www.youtube.com/watch?v=vid1"
        assert data["items"][1]["duration_formatted"] == "05:00"

def test_expand_playlist_single_video_fallback():
    mock_single_data = {
        "title": "Single Video",
        "uploader": "Solo Creator",
        "total_items": 1,
        "items": [
            {
                "title": "Single Video",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "duration": 213.0,
                "duration_formatted": "03:33",
                "thumbnail": "https://img.youtube.com/vi/dQw4w9WgXcQ/default.jpg",
                "uploader": "Solo Creator"
            }
        ]
    }

    with mock.patch.object(YtDlpService, "extract_playlist", return_value=mock_single_data):
        response = client.post("/api/v1/batch/expand-playlist", json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total_items"] == 1
        assert data["items"][0]["title"] == "Single Video"

def test_expand_playlist_empty_url():
    response = client.post("/api/v1/batch/expand-playlist", json={
        "url": "   "
    })
    assert response.status_code == 400
    assert "valid URL" in response.json()["detail"]

def test_expand_playlist_error_handling():
    with mock.patch.object(YtDlpService, "extract_playlist", side_effect=ValueError("Invalid playlist")):
        response = client.post("/api/v1/batch/expand-playlist", json={
            "url": "https://www.youtube.com/playlist?list=INVALID"
        })
        assert response.status_code == 400
        assert "Failed to expand playlist" in response.json()["detail"]

def test_ytdlp_extract_playlist_unit():
    # Test internal extraction logic with mocked yt_dlp.YoutubeDL
    mock_ydl_instance = mock.MagicMock()
    mock_ydl_instance.extract_info.return_value = {
        "title": "Awesome Mix",
        "uploader": "DJ Sol",
        "entries": [
            {
                "id": "abc1234",
                "title": "Track 1",
                "duration": 60,
                "thumbnail": "http://example.com/thumb1.jpg"
            },
            {
                "id": "def5678",
                "title": "Track 2",
                "url": "https://www.youtube.com/watch?v=def5678",
                "duration": 180,
                "thumbnail": "http://example.com/thumb2.jpg"
            },
            None
        ]
    }

    with mock.patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance
        res = YtDlpService.extract_playlist("https://youtube.com/playlist?list=xyz", max_items=5)
        assert res["title"] == "Awesome Mix"
        assert res["total_items"] == 2
        assert res["items"][0]["url"] == "https://www.youtube.com/watch?v=abc1234"
        assert res["items"][0]["duration_formatted"] == "01:00"
        assert res["items"][1]["url"] == "https://www.youtube.com/watch?v=def5678"
