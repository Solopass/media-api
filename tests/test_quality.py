from unittest import mock

import pytest
from pydantic import ValidationError

from app.models.schemas import ClipRequest, DownloadRequest, IngestRequest
from app.services.ytdlp_service import YtDlpService, _video_format_selector


def test_best_quality_has_no_height_cap():
    assert _video_format_selector("best") == "bv*+ba/b"


@pytest.mark.parametrize("quality,height", [("2160p", 2160), ("1080p", 1080), ("360p", 360)])
def test_capped_quality_limits_height_with_fallback(quality, height):
    sel = _video_format_selector(quality)
    assert f"height<={height}" in sel
    assert sel.endswith("/bv*+ba/b")


def test_requests_default_to_highest_quality():
    assert DownloadRequest(url="u").quality.value == "best"
    assert ClipRequest(source="u", start_time="0", end_time="5").quality.value == "best"
    assert IngestRequest(source="u").quality is None


def test_unknown_quality_is_rejected():
    with pytest.raises(ValidationError):
        DownloadRequest(url="u", quality="8k")


def _download_opts(tmp_path, **kwargs):
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {"title": "t", "requested_downloads": [{"filepath": str(tmp_path / "t.mkv")}]}

        def prepare_filename(self, info):
            return str(tmp_path / "t.mp4")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        result = YtDlpService.download("https://example.com/v", custom_out_dir=tmp_path, **kwargs)
    return captured, result


def test_download_defaults_to_best_and_reports_merged_path(tmp_path):
    opts, result = _download_opts(tmp_path)
    assert opts["format"] == "bv*+ba/b"
    assert opts["merge_output_format"] == "mp4/mkv"
    assert result["file_path"].endswith("t.mkv")


def test_download_applies_quality_cap_and_webm_container(tmp_path):
    opts, _ = _download_opts(tmp_path, quality="720p", target_format="webm")
    assert "height<=720" in opts["format"]
    assert opts["merge_output_format"] == "webm/mkv"


def test_audio_only_ignores_quality(tmp_path):
    opts, _ = _download_opts(tmp_path, audio_only=True, quality="360p")
    assert opts["format"] == "bestaudio/best"
