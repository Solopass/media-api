"""
X (Twitter) support, and the three real failures it would otherwise hit.

1. An X profile URL matched none of the YouTube-shaped collection markers, so it
   passed the ingest gate, queued a job, and died at download with a raw
   "ERROR: Unsupported URL". X publishes no timeline, so there is nothing to
   expand either -- it has to be refused with a reason.
2. `noplaylist` does not collapse an X post holding several videos: yt-dlp's
   _yes_playlist() returns True when the URL names no specific video, so the
   result is a playlist with no `requested_downloads` of its own, and download()
   fell through to prepare_filename() -- a path nothing ever wrote.
3. Downloading from X needs no account, and must not be able to use one. A
   cookies.txt present for another site must never reach an X request.
"""

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import IngestRequest, PlaylistItem
from app.services.ffmpeg_service import FFmpegService
from app.services.ytdlp_service import (
    YtDlpService, _base_opts, _video_format_selector, is_collection_url,
    x_friendly_error, x_url_kind, X_PHOTO, X_SINGLE, X_UNSUPPORTED,
)

client = TestClient(app)

POST = "https://x.com/historyinmemes/status/1790637656616943991"


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://x.com/someone/status/1790637656616943991",
    "https://twitter.com/someone/status/1790637656616943991",
    "https://www.x.com/someone/status/123",
    "https://mobile.twitter.com/someone/status/123",
    "https://x.com/i/web/status/123",
    "https://x.com/statuses/123",
    "https://x.com/someone/status/123/video/2",
    "https://x.com/i/videos/tweet/123",
    "https://x.com/i/cards/tfw/v1/123",
    "https://x.com/i/broadcasts/abc123",
    "https://x.com/i/events/abc123",
    "https://x.com/i/spaces/1AbCdEfGhIjKl",
    "https://t.co/shortkey",
    "https://amp.twimg.com/v/93ad3568-03d2-47b9-a57b-624b88693acd",
])
def test_single_post_shapes_are_ingestible(url):
    assert x_url_kind(url) == X_SINGLE


@pytest.mark.parametrize("url", [
    "https://x.com/historyinmemes",
    "https://x.com/historyinmemes/media",
    "https://x.com/historyinmemes/with_replies",
    "https://x.com/search?q=cats",
    "https://x.com/i/lists/12345",
    "https://x.com/i/bookmarks",
    "https://x.com/home",
    "https://x.com/explore",
    "https://x.com/",
])
def test_timeline_shapes_are_refused(url):
    """X has no public timeline, so none of these can be read or expanded."""
    assert x_url_kind(url) == X_UNSUPPORTED


def test_photo_links_are_their_own_case():
    assert x_url_kind("https://x.com/someone/status/123/photo/1") == X_PHOTO


def test_non_x_urls_are_not_classified():
    for url in ["https://youtube.com/watch?v=1", "https://example.com/v.mp4", "", "not a url"]:
        assert x_url_kind(url) is None


@pytest.mark.parametrize("url", [
    POST,
    "https://x.com/c/status/5",       # the handle @c -- contains the "/c/" marker
    "https://x.com/user/status/5",    # the handle @user -- contains "/user/"
])
def test_x_posts_are_never_read_as_collections(url):
    """
    The YouTube markers would misread these: a post by @c is /c/status/<id>.
    Sending one to Batch mode would be wrong twice over, since Batch cannot
    expand an X profile either.
    """
    assert is_collection_url(url) is False


# --- cookie policy ----------------------------------------------------------

def test_x_requests_never_carry_cookies(tmp_path, monkeypatch):
    """
    The guarantee: an X request cannot pick up the user's session, even once a
    cookies.txt exists in the cache dir for some other site.
    """
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    assert "cookiefile" not in _base_opts(POST)
    assert "cookiefile" not in _base_opts("https://twitter.com/a/status/1")
    assert "cookiefile" not in _base_opts("https://x.com/historyinmemes")
    # Non-X hosts are unaffected.
    assert _base_opts("https://youtube.com/watch?v=1")["cookiefile"] == str(tmp_path / "cookies.txt")


def test_no_cookie_file_means_no_cookiefile_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    assert "cookiefile" not in _base_opts(POST)
    assert "cookiefile" not in _base_opts("https://youtube.com/watch?v=1")


def test_base_opts_carries_only_what_every_caller_can_take(tmp_path, monkeypatch):
    """
    download() shares these options, so anything call-site specific leaking in
    would break it -- a stray skip_download most of all. Asserting the whole key
    set means a future addition has to be considered here rather than silently
    reaching every caller.
    """
    monkeypatch.setattr(settings, "cache_dir", tmp_path)
    assert set(_base_opts(POST)) <= {"quiet", "no_warnings", "js_runtimes"}

    (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert set(_base_opts("https://youtube.com/watch?v=1")) <= {
        "quiet", "no_warnings", "js_runtimes", "cookiefile",
    }


# --- quality ----------------------------------------------------------------

def test_x_defaults_to_the_highest_rendition():
    """
    X tops out near 720p, so a cap costs visible quality for almost no
    bandwidth. Progressive is preferred over HLS: same height, no ffmpeg merge.
    """
    assert _video_format_selector("best", POST) == "b[protocol^=http]/bv*+ba/b"


def test_an_explicit_cap_still_applies_on_x():
    sel = _video_format_selector("480p", POST)
    assert "height<=480" in sel
    assert sel.startswith("b[protocol^=http][height<=480]")


def test_non_x_quality_selection_is_unchanged():
    assert _video_format_selector("best", "https://youtube.com/watch?v=1") == "bv*+ba/b"
    assert _video_format_selector("best") == "bv*+ba/b"


def test_ingest_request_accepts_a_video_choice():
    assert IngestRequest(source=POST).playlist_index is None
    assert IngestRequest(source=POST, playlist_index=2).playlist_index == 2


# --- multi-video posts ------------------------------------------------------

def _fake_extract(payload):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return payload

    return mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL)


# Mirrors a real X playlist result: entries are fully-extracted dicts with no
# "url" key, "id" is the media id (not the status id), and photos have already
# been filtered out, so playlist_index is the position among the videos.
X_PLAYLIST = {
    "title": "A post with two videos",
    "uploader": "someone",
    "entries": [
        {"id": "1578400766095654915", "title": None, "duration": 12.0, "playlist_index": 1},
        {"id": "1578391494028890119", "title": None, "duration": 8.5, "playlist_index": 2},
    ],
}


def test_each_video_in_a_post_becomes_a_pickable_row():
    with _fake_extract(X_PLAYLIST):
        data = YtDlpService.extract_playlist(POST)

    assert data["total_items"] == 2
    assert [item["playlist_index"] for item in data["items"]] == [1, 2]
    # Every video shares the post's URL, so the index is what tells them apart.
    assert all(item["url"] == POST for item in data["items"])
    # The rows must survive the response model.
    assert [PlaylistItem(**item).playlist_index for item in data["items"]] == [1, 2]


def test_a_text_less_post_is_not_mistaken_for_a_deleted_video():
    """
    is_unavailable_entry() reads a missing title as "deleted", which is true of
    YouTube's flat entries and not of X: a post with no text has no title.
    """
    with _fake_extract(X_PLAYLIST):
        data = YtDlpService.extract_playlist(POST)

    assert data["skipped_unavailable"] == 0
    assert all(item["title"] for item in data["items"]), "rows need a label to be pickable"


def test_x_rows_never_get_a_rebuilt_youtube_url():
    with _fake_extract(X_PLAYLIST):
        data = YtDlpService.extract_playlist(POST)

    assert not any("youtube.com" in item["url"] for item in data["items"])


def test_a_single_video_post_needs_no_index():
    with _fake_extract({"title": "One video", "uploader": "someone", "duration": 15.5}):
        data = YtDlpService.extract_playlist(POST)

    assert data["total_items"] == 1
    assert data["items"][0]["playlist_index"] is None
    assert data["items"][0]["url"] == POST


def test_a_chosen_video_is_requested_by_position(tmp_path):
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {"title": "t", "requested_downloads": [{"filepath": str(tmp_path / "t.mp4")}]}

        def prepare_filename(self, info):
            return str(tmp_path / "wrong.mp4")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        YtDlpService.download(POST, custom_out_dir=tmp_path, playlist_index=2)

    assert captured["playlist_items"] == "2"


def test_a_multi_video_post_yields_a_real_file_not_a_guessed_name(tmp_path):
    """
    The regression that matters: a playlist result has no requested_downloads of
    its own, so reading them off the top level produced prepare_filename()'s
    name -- a file nothing wrote, handed straight to the clipper.
    """
    real = tmp_path / "first.mp4"
    real.write_bytes(b"\x00")

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {
                "title": "post",
                "entries": [
                    {"title": "first", "duration": 12.0,
                     "requested_downloads": [{"filepath": str(real)}]},
                    {"title": "second", "duration": 8.5,
                     "requested_downloads": [{"filepath": str(tmp_path / "second.mp4")}]},
                ],
            }

        def prepare_filename(self, info):
            return str(tmp_path / "never-written.mp4")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        result = YtDlpService.download(POST, custom_out_dir=tmp_path)

    assert result["file_path"] == str(real)
    assert Path(result["file_path"]).exists(), "the pipeline clips this path, so it has to exist"
    assert result["total_videos"] == 2
    assert result["title"] == "first"
    assert result["duration"] == 12.0


# --- endpoint behaviour -----------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://x.com/historyinmemes",
    "https://x.com/i/bookmarks",
    "https://x.com/search?q=cats",
])
def test_ingest_refuses_a_timeline_url_with_a_reason(url):
    with mock.patch("app.routers.ingest.job_manager.submit_pipeline") as submit:
        res = client.post("/api/v1/ingest", json={"source": url})

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "individual X posts" in detail
    assert "status" in detail, "the message should show the shape of a usable link"
    submit.assert_not_called()


def test_ingest_refuses_a_photo_link():
    with mock.patch("app.routers.ingest.job_manager.submit_pipeline") as submit:
        res = client.post("/api/v1/ingest", json={"source": "https://x.com/a/status/1/photo/1"})

    assert res.status_code == 400
    assert "photo" in res.json()["detail"]
    submit.assert_not_called()


def test_expanding_a_timeline_url_is_refused_too():
    """Batch is not an escape hatch: there is no public timeline to expand."""
    res = client.post("/api/v1/batch/expand-playlist",
                      json={"url": "https://x.com/historyinmemes"})
    assert res.status_code == 400
    assert "individual X posts" in res.json()["detail"]


def test_probe_reports_how_many_videos_a_post_holds():
    with _fake_extract(X_PLAYLIST):
        res = client.get("/api/v1/media/probe", params={"url": POST})

    assert res.status_code == 200
    body = res.json()
    assert body["video_count"] == 2
    # The preview describes the first video, not the empty playlist wrapper.
    assert body["duration"] == 12.0


def test_probe_reports_one_video_for_an_ordinary_post():
    with _fake_extract({"title": "One video", "uploader": "someone", "duration": 15.5}):
        res = client.get("/api/v1/media/probe", params={"url": POST})

    assert res.status_code == 200
    assert res.json()["video_count"] == 1


# --- silent video (X "GIFs") ------------------------------------------------

def test_a_video_with_no_audio_track_is_detected(tmp_path):
    f = tmp_path / "gif.mp4"
    f.write_bytes(b"\x00")

    with mock.patch("app.services.ffmpeg_service.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        assert FFmpegService.has_audio_stream(f) is False

        run.return_value = mock.Mock(returncode=0, stdout="1\n", stderr="")
        assert FFmpegService.has_audio_stream(f) is True


def test_a_failed_probe_does_not_mark_a_video_silent(tmp_path):
    """
    Guessing "no audio" from a broken ffprobe would silently skip transcription
    on a perfectly normal video, so the check fails safe.
    """
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00")

    with mock.patch("app.services.ffmpeg_service.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
        assert FFmpegService.has_audio_stream(f) is True

        run.side_effect = OSError("ffprobe missing")
        assert FFmpegService.has_audio_stream(f) is True


# --- error translation ------------------------------------------------------
# Not every X failure can be spotted before the request goes out: a post whose
# video was deleted, or one that needs a sign-in, only fails once yt-dlp asks.

def test_a_post_without_a_video_says_so():
    err = Exception("ERROR: [twitter] 1696256659889565950: No video could be found in this tweet")
    msg = x_friendly_error(POST, err)
    assert msg and "no video" in msg.lower()
    assert "twitter]" not in msg, "the raw extractor text should not reach the user"


def test_an_unsupported_url_error_explains_the_timeline_limit():
    msg = x_friendly_error("https://x.com/historyinmemes", Exception("ERROR: Unsupported URL: https://x.com/historyinmemes"))
    assert msg and "individual X posts" in msg


def test_a_sign_in_wall_is_named_as_the_accepted_limit():
    for text in [
        "ERROR: [twitter] 1: NSFW tweet requires authentication",
        "ERROR: [twitter] 1: You must log in to view this",
        "ERROR: [twitter] 1: age-restricted content",
    ]:
        msg = x_friendly_error(POST, Exception(text))
        assert msg and "signed-in" in msg


def test_unrecognised_errors_keep_their_own_message():
    """Inventing an explanation would be worse than showing the real one."""
    assert x_friendly_error(POST, Exception("HTTP Error 503: Service Unavailable")) is None


def test_non_x_errors_are_never_rewritten():
    err = Exception("ERROR: Unsupported URL: https://example.com/x")
    assert x_friendly_error("https://example.com/x", err) is None
    assert x_friendly_error("https://youtube.com/watch?v=1", Exception("No video could be found")) is None


def test_a_silent_video_survives_the_cleanup(tmp_path):
    """
    The regression that would destroy the only artifact: with no audio there is
    no extracted file to keep back, so passing keep=None to the cleanup deleted
    the video too and left a note pointing at nothing.
    """
    from app.routers.ingest import run_ingest_pipeline

    media = tmp_path / "gif.mp4"
    media.write_bytes(b"\x00" * 64)
    note = tmp_path / "note.md"
    note.write_text("note", encoding="utf-8")

    # The cleanup refuses to touch anything outside the dirs this API writes
    # into, so without this the file would survive whatever the keep logic did
    # and the test would prove nothing.
    with mock.patch("app.routers.ingest.MANAGED_MEDIA_DIRS", (tmp_path,)), \
         mock.patch.object(YtDlpService, "download", return_value={
             "title": "a looping gif", "duration": 3.0, "uploader": "someone",
             "file_path": str(media), "url": POST, "total_videos": 1,
         }), mock.patch.object(FFmpegService, "has_audio_stream", return_value=False), \
         mock.patch("app.routers.ingest.ObsidianService.create_transcript_note", return_value=note), \
         mock.patch("app.routers.ingest.stt_service.transcribe") as transcribe, \
         mock.patch("app.routers.ingest.summarize_with_ollama") as summarize:
        run_ingest_pipeline("job-silent", IngestRequest(source=POST, keep_video=False))

    assert media.exists(), "the silent video is the only media the run produced"
    transcribe.assert_not_called()
    summarize.assert_not_called()


def test_a_normal_video_still_gets_cleaned_up(tmp_path):
    """The counterpart: with audio, the video is still discarded as before."""
    from app.routers.ingest import run_ingest_pipeline

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\x00" * 8)
    note = tmp_path / "note.md"
    note.write_text("note", encoding="utf-8")

    with mock.patch("app.routers.ingest.MANAGED_MEDIA_DIRS", (tmp_path,)), \
         mock.patch.object(YtDlpService, "download", return_value={
             "title": "a talking video", "duration": 30.0, "uploader": "someone",
             "file_path": str(media), "url": POST, "total_videos": 1,
         }), mock.patch.object(FFmpegService, "has_audio_stream", return_value=True), \
         mock.patch.object(FFmpegService, "extract_audio", return_value=audio), \
         mock.patch("app.routers.ingest.ObsidianService.create_transcript_note", return_value=note), \
         mock.patch("app.routers.ingest.stt_service.transcribe", return_value={
             "full_text": "hello", "segments": [], "duration": 30.0,
             "language": "en", "model": "parakeet",
         }), \
         mock.patch("app.routers.ingest.summarize_with_ollama", return_value="summary"):
        run_ingest_pipeline("job-normal", IngestRequest(source=POST, keep_video=False))

    assert not media.exists(), "the source video is bulk once the audio exists"
    assert audio.exists(), "the extracted audio is what the player streams"


def test_the_real_total_survives_picking_one_video(tmp_path):
    """
    playlist_items narrows the entries to the chosen one, so counting them would
    report 1. playlist_count is the post's real total, which is why it wins.
    """
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {
                "title": "post",
                "playlist_count": 4,
                "entries": [
                    {"title": "third", "duration": 5.0,
                     "requested_downloads": [{"filepath": str(tmp_path / "third.mp4")}]},
                ],
            }

        def prepare_filename(self, info):
            return str(tmp_path / "never-written.mp4")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        result = YtDlpService.download(POST, custom_out_dir=tmp_path, playlist_index=3)

    assert result["total_videos"] == 4, "the post holds four, even though one was taken"
    assert result["title"] == "third"


def test_no_item_is_requested_when_none_was_chosen(tmp_path):
    """
    A stray playlist_items would change what every other source downloads, so it
    must be absent unless a video was actually picked.
    """
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {"title": "t", "requested_downloads": [{"filepath": str(tmp_path / "t.mp4")}]}

        def prepare_filename(self, info):
            return str(tmp_path / "t.mp4")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        YtDlpService.download("https://youtube.com/watch?v=1", custom_out_dir=tmp_path)

    assert "playlist_items" not in captured


def test_rows_fall_back_to_their_position_without_an_index():
    """Older yt-dlp builds may not set playlist_index on every entry."""
    payload = {
        "title": "post",
        "entries": [
            {"id": "a", "title": "one", "duration": 1.0},
            {"id": "b", "title": "two", "duration": 2.0},
        ],
    }
    with _fake_extract(payload):
        data = YtDlpService.extract_playlist(POST)

    assert [item["playlist_index"] for item in data["items"]] == [1, 2]


def test_a_non_x_playlist_probe_is_left_alone():
    """
    probe_url sets no noplaylist, so a YouTube playlist arrives with entries
    too. Collapsing those would change the preview card for every playlist and
    claim a picker on the Download tab, which has no batch mode.
    """
    payload = {
        "title": "Top Trending Videos of the Week",
        "uploader": "YouTube",
        "playlist_count": 19,
        "entries": [{"id": "x", "title": "first video", "duration": 61.0}],
    }
    with _fake_extract(payload):
        data = YtDlpService.probe_url("https://www.youtube.com/playlist?list=PL123")

    assert data["video_count"] == 1, "only X reports a count; others keep the default"
    assert data["title"] == "Top Trending Videos of the Week", "the playlist's own title stands"


def test_expanding_a_post_with_no_video_gives_the_reason_not_the_raw_error():
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise Exception("ERROR: [twitter] 123: No video could be found in this tweet")

    with mock.patch("app.services.ytdlp_service.yt_dlp.YoutubeDL", FakeYDL):
        res = client.post("/api/v1/batch/expand-playlist", json={"url": POST})

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "no video" in detail.lower()
    assert "Failed to expand playlist" not in detail
