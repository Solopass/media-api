# 📹 Workstation Linux Media API Reference

> REST API for automated YouTube and web media downloads, precision video/audio clipping with EBU R128 normalization and 16kHz speech downmixing, speech-to-text transcription (NVIDIA Parakeet by default, `faster-whisper` for multilingual fallback), AI-powered structured summaries, and direct Obsidian vault ingestion.

---

## ⚙️ Speech-to-Text Engines

- **`parakeet`** (default): `parakeet-tdt-0.6b-v3` q8 via Windows `parakeet-cli.exe` (WSL interop).
  - Most accurate in `D:\OBVLT\reports\stt-bench.md` (4.8% WER vs 5.7% for whisper tiny, superior on proper nouns/names).
  - ~19x realtime at 8 threads.
  - Covers 25 European languages (`en`, `de`, `es`, `fr`, `it`, `pt`, `nl`, `ru`, `uk`, `pl`, `sv`, `da`, `fi`, `cs`, `el`, `hu`, `ro`, `sk`, `sl`, `bg`, `hr`, `lt`, `lv`, `et`, `mt`).
- **`whisper`**: `faster-whisper` on CPU (`model_size`: `tiny`, `base`, `small`, `medium`).
  - Automatically activated whenever `language` is set to one Parakeet does not support (e.g. `ja`, `zh`, `ar`, `ko`, `hi`).
- Configure per request with `"engine"`, or change default via `MEDIA_API_STT_ENGINE`.
- `MEDIA_API_PARAKEET_THREADS` (default `8`) constrains CPU thread allocation.

---

## ⚡ Complete Endpoints Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/health` | Health audit, core affinity, active jobs, idle seconds, and D: storage stats |
| `POST` | `/api/v1/config/idle-timeout` | Dynamically update idle auto-shutdown timeout and auto-close state |
| `GET` | `/api/v1/models` | Query local Ollama daemon for available LLM models |
| `GET` | `/api/v1/media/probe` | Pre-fetch URL or local media metadata (title, uploader, duration, thumbnail) |
| `POST` | `/api/v1/download` | Asynchronously download YouTube or web media to `D:\Output\Videos\` |
| `POST` | `/api/v1/clip` | Slice media with optional EBU R128 loudness normalizer and 16kHz speech WAV downmixing |
| `POST` | `/api/v1/transcribe` | Transcribe media (Parakeet or `faster-whisper`) |
| `POST` | `/api/v1/ingest` | One-shot pipeline: Download → Clip → Extract Audio → STT (chunked for long media) → Ollama Summary → Obsidian Note |
| `POST` | `/api/v1/batch/expand-playlist` | Fast flat-extraction of YouTube playlists, channels, or video metadata for batch ingestion (<2s) |
| `POST` | `/api/v1/vault/resummarize` | Regenerate or change summary preset for existing Obsidian note in-place without re-transcribing |
| `POST` | `/api/v1/vault/chapters` | Generate validated YouTube-style chapter markers for an existing note |
| `GET` | `/api/v1/storage` | Per-category breakdown of the output directories, plus D: usage |
| `POST` | `/api/v1/storage/reclaim` | Delete old generated media by age (dry run by default; sidecars and notes never touched) |
| `GET` | `/api/v1/diagnostics` | Recent warnings and errors, plus the log file location and an optional tail |
| `POST` | `/api/v1/client-log` | Record a browser-side failure in the server log (buffered and flushed if the server was down) |
| `POST` | `/api/v1/upload` | Stream uploaded media directly into `D:\AI\Cache\uploads\` |
| `GET` | `/api/v1/media/stream` | Stream audio/video with HTTP Range byte seeking support |
| `GET` | `/api/v1/vault/recent` | Recursively scan `D:\AI\Vault\Transcripts` for recent transcript notes |
| `GET` | `/api/v1/vault/note` | Retrieve note content, wikilinks, and Obsidian desktop URI |
| `GET` | `/api/v1/status/{job_id}` | Poll asynchronous job status, progress %, ETA, and output file paths |
| `GET` | `/api/v1/jobs` | View queue history of all recent workstation operations |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Cancel a queued or running job, terminating the engine process within ~1s |
| `POST` | `/api/v1/jobs/clear` | Purge completed, failed, and cancelled jobs from history |
| `POST` | `/api/v1/shutdown` | Gracefully terminate worker immediately to release host RAM and threads |
| `GET` | `/ui` | Full-featured HTML5 Single-Page Web Studio interface |

---

## 📝 Request Schemas & Parameters

### 1. `POST /api/v1/ingest`
One-shot ingestion pipeline:
```json
{
  "source": "https://www.youtube.com/watch?v=...",
  "clip_start": "00:01:00",
  "clip_end": "00:05:00",
  "engine": "parakeet",
  "model_size": "tiny",
  "language": null,
  "summarize": true,
  "llm_model": "sol-fast",
  "summary_preset": "executive",
  "summary_focus": "key architectural decisions and performance metrics",
  "vault_subfolder": "Transcripts/Meetings",
  "tags": ["architecture", "review"],
  "keep_video": false,
  "generate_chapters": false
}
```
**Source Video Retention** (`keep_video`, default `false`):
- `false`: the video **this job produced** is deleted once the vault note is written — a yt-dlp download, a clip intermediate, or an upload-cache copy from drag-and-drop.
- `true`: the video is kept.
- The extracted audio and `.transcript.json` sidecar are **always** kept — the UI audio player streams them back.
- Files the job did not produce are never deleted, even inside `D:\Output\Videos`. Re-ingesting a video you parked there by path leaves it exactly where it is.
- Cancelled and failed jobs also reclaim their own download, so an aborted run does not strand gigabytes.

**Language** (`language`, default `null` = auto-detect): a code such as `en`, `es`, `ja`. Parakeet covers 25 European languages; any other code routes the job to faster-whisper automatically.

**Summary Presets**:
- `executive`: Core Briefing, Key Themes & Decisions, Deliverables & Actionable Takeaways.
- `meeting`: Attendees, Agenda Topics, Decisions Reached, Action Item Ownership Table (`| Owner | Action Item | Target |`).
- `timeline`: Chronological Topic Breakdown & Timeline tracking major topic shifts.
- `technical`: Architectural & System Overview, APIs/Schemas, Technical Trade-offs, Implementation Notes.
- `bullets`: Dense, high-signal bulleted takeaways with bold lead-ins.

### 2. `POST /api/v1/clip`
Precision audio/video extraction:
```json
{
  "source": "/mnt/d/Output/Videos/presentation.mp4",
  "start_time": "00:02:15",
  "end_time": "00:04:45",
  "format": "mp4",
  "re_encode": false,
  "normalize_audio": true,
  "speech_optimized": false
}
```
- `normalize_audio`: Applies dual-pass EBU R128 filter (`loudnorm=I=-16:TP=-1.5:LRA=11`).
- `speech_optimized`: Downmixes to 16,000 Hz 16-bit mono PCM WAV for ultra-fast STT inference.

### 3. `POST /api/v1/config/idle-timeout`
Dynamically tune the auto-shutdown watchdog:
```json
{
  "timeout_seconds": 300,
  "auto_close": true
}
```
Set `timeout_seconds: 0` or `auto_close: false` for **Keep Awake** mode.

### 4. `GET /api/v1/media/probe?url=...`
Instant flat metadata extraction (sub-second):
```json
{
  "title": "Lex Fridman Podcast #420",
  "uploader": "Lex Fridman",
  "duration": 9482.0,
  "duration_formatted": "158:02",
  "thumbnail": "https://i.ytimg.com/vi/.../maxresdefault.jpg",
  "description": "..."
}
```

### 5. `POST /api/v1/batch/expand-playlist`
Fast flat extraction of a YouTube playlist, channel, or video URL into individual items (<2s) for batch ingestion:
```json
{
  "url": "https://www.youtube.com/playlist?list=PL12345",
  "max_items": 50
}
```
`max_items` accepts 1-500 (default 50); out-of-range values return `422`.

Private, deleted and region-blocked entries are dropped from `items` and counted in
`skipped_unavailable`. yt-dlp reports them with every field empty except the id, so they
cannot be downloaded; including them would queue guaranteed failures.

Note that `POST /api/v1/ingest` **rejects** playlist and channel URLs with `400`: they
expand to many videos, and the single-item pipeline would otherwise hand the collection to
yt-dlp and fail on whichever entry it picked. Expand them here first, or let the UI switch
to Batch mode for you.

**Response**:
```json
{
  "title": "AI & Systems Playlist",
  "uploader": "Creator",
  "total_items": 2,
  "items": [
    {
      "title": "Episode 1: Architecture Deep Dive",
      "url": "https://www.youtube.com/watch?v=vid1",
      "duration": 1820.0,
      "duration_formatted": "30:20",
      "thumbnail": "https://i.ytimg.com/vi/vid1/maxresdefault.jpg",
      "uploader": "Creator"
    }
  ]
}
```

### 6. `POST /api/v1/vault/resummarize`
Regenerate or switch summary presets for an existing markdown note without re-downloading or re-transcribing:
```json
{
  "note_path": "2026-09-18_173536_Autistic_Behaviors.md",
  "llm_model": "sol-fast",
  "summary_preset": "technical",
  "summary_focus": "key architectures and trade-offs"
}
```
The target must resolve inside the Obsidian vault roots (`D:\AI\Vault`, `D:\OBVLT`, `D:\Output\Transcripts`) and must be a `.md` file; anything else returns `403` or `400`. This endpoint rewrites the file in place.

Replaces the `> [!abstract]` callout in-place while preserving YAML frontmatter, headers, and timestamped transcripts.

### 7. `POST /api/v1/vault/chapters`
Generate YouTube-style chapter markers for an existing transcript note, in place:
```json
{
  "note_path": "2026-09-18_170027_Autistic_Dating_Differences_Explained.md",
  "llm_model": "sol-fast"
}
```
**Response** carries both the structured chapters and a paste-ready block:
```json
{
  "success": true,
  "chapters": [
    { "start": 0.0, "timestamp": "00:00", "title": "Hydration Reminder" },
    { "start": 94.0, "timestamp": "01:34", "title": "Autistic Dating Overview" }
  ],
  "youtube_block": "00:00 Hydration Reminder\n01:34 Autistic Dating Overview",
  "model": "sol-fast"
}
```
How it stays accurate:
- The model is shown a **timestamped digest** — the transcript downsampled into
  `[MM:SS] text` lines spread evenly across the whole recording, so chapters cover the
  middle of a long video instead of clustering where the text happened to be truncated.
- **Nothing returned is trusted**: timestamps past the real duration are dropped, order is
  repaired, near-duplicates are merged, the first chapter is forced to `00:00`, and the
  count is capped at 20. Fewer than 2 survivors returns `422` rather than writing a bad
  chapter list into the vault.
- Segments come from the `.transcript.json` sidecar, falling back to the note's own
  timestamped table when the sidecar is gone.
- Same guards as `/vault/resummarize`: vault roots and `.md` only (`403` / `400`), and
  `404` when the note does not exist.

Chapters are written to the note in **both** forms — a `chapters:` YAML list in the
frontmatter and a readable `## 🎬 Chapters` section — and added to the sidecar. Re-running
replaces both rather than duplicating.

During ingest, set `"generate_chapters": true` (a second local LLM pass; off by default).

### 8. `GET /api/v1/diagnostics` & `POST /api/v1/client-log`
Failure visibility, for when something breaks and the evidence would otherwise be gone.

- The server logs to `D:\AI\Cache\logs\media-api.log` (rotating, 2 MB x 3) in addition to
  stdout/journald, including uvicorn request errors and access lines, and any unhandled
  exception with its traceback.
- `POST /api/v1/client-log` takes `{level, message, context?, url?, stack?}` from the UI.
  The page reports failed fetches, batch item failures and uncaught JS errors here. When
  the server is unreachable at that moment — the WSL VM being torn down mid-request is the
  common case — the report is buffered in `localStorage` and flushed on the next
  successful health poll, tagged with when it originally happened.
- `GET /api/v1/diagnostics?limit=50&level=WARNING&tail_lines=0` returns the recent
  warnings and errors held in memory, the log file path and size, and optionally the last
  N lines of the file.

### 9. `GET /api/v1/storage` & `POST /api/v1/storage/reclaim`
Per-category accounting of the API's own output directories, plus age-based cleanup.

`GET /api/v1/storage` (add `?refresh=true` to bypass the 30s scan cache):
```json
{
  "drive": { "path": "/mnt/d", "total_gb": 1863.02, "used_gb": 287.48, "free_gb": 1575.53 },
  "categories": [
    {
      "name": "audio",
      "label": "Extracted Audio",
      "windows_directory": "D:\\Output\\Audio",
      "file_count": 2,
      "total_bytes": 68063683,
      "total_mb": 64.9,
      "oldest_modified": "2026-09-18T16:57:40",
      "reclaimable": true
    }
  ],
  "reclaimable_by_age": { "7": {...}, "30": {...}, "90": {...} },
  "scanned_at": "2026-09-19T03:22:10"
}
```
Categories: `videos`, `audio`, `uploads` (reclaimable) and `sidecars`, `note_mirrors`
(reported only, never deleted).

`POST /api/v1/storage/reclaim`:
```json
{
  "older_than_days": 30,
  "categories": ["videos", "audio", "uploads"],
  "dry_run": true
}
```
- **`dry_run` defaults to `true`** and returns the candidate list without deleting, so the
  UI previews first and re-posts with `dry_run: false` to act.
- `.transcript.json` sidecars and `.md` notes are never candidates, whatever categories are
  passed — they are tiny and power the transcript view, re-summarization and chapters.
- Files listed in the `output_files` of a queued or running job are skipped.
- A protected or unknown category name returns `422`; `older_than_days` accepts 0-3650.

Responses carry exact byte counts (`total_bytes`, `freed_bytes`) alongside the rounded MB.

### 10. `GET /api/v1/models` (GPU VRAM & Model Headroom)
Returns available Ollama LLM models enriched with memory footprint assessments against dedicated GPU VRAM:
```json
{
  "models": [
    {
      "name": "sol-fast:latest",
      "size_gb": 11.8,
      "required_vram_gb": 13.6,
      "vram_status": "fits",
      "details": { "family": "llama", "parameter_size": "20B", "quantization_level": "Q4_K_M" }
    }
  ],
  "default": "sol-fast:latest",
  "gpu": {
    "adapter_name": "AMD Radeon RX 9070 XT",
    "dedicated_vram_gb": 15.9,
    "headroom_gb": 2.3
  }
}
```
`vram_status` values:
- `fits`: Comfortably fits within dedicated VRAM with >= 1.5 GB headroom.
- `tight`: Fits with marginal headroom (< 1.5 GB).
- `wont_fit`: Exceeds dedicated VRAM and will spill into shared host system RAM.

---

## 🎧 Transcription Behaviour (Long Audio, Progress, Cancellation)

### Chunking

Recordings longer than **20 minutes** are split before transcription and stitched back
together. This is not an optimisation — `parakeet-cli` cannot decode a long recording at
all. Measured on this workstation:

| Audio | Result |
| :--- | :--- |
| 9h05m @ 44.1 kHz stereo (499 MB) | `exit 5` in `read_audio_data` after 48s |
| 9h05m @ 16 kHz mono (93.6 MB) | `exit 5` after 28s — so it is duration, not format |
| 4h, 2h, 1h, 30m | decode fine |

9.08h × 16 kHz × 4 bytes = 1.95 GB, right at a 2 GB float32 ceiling.

How the split is made:
- One `ffmpeg silencedetect` pass supplies candidate cut points.
- Cuts target 20-minute boundaries and **snap to the nearest silence within ±20s**, so a
  sentence is not sliced mid-word. Without a nearby silence the cut lands on the target.
- Chunks are written as 16 kHz mono wav into the job's temp directory and removed with it.
- Segment timestamps are shifted by each chunk's start and ids renumbered, so the note's
  transcript table is continuous. The response gains a `chunks` count.

Throughput on this workstation: **~20x realtime** (a 20-minute chunk in ~59s), so a
9-hour book is roughly 28 minutes of transcription.

### Progress semantics

`JobDetail.speed` carries a human-readable position and `progress_percent` the overall bar.
Two kinds of number appear, and they are deliberately distinguishable:

- **Measured** — a completed chunk, or a real ffmpeg position from its `-progress` stream.
- **Estimated** — a position *within* a chunk, extrapolated from this workstation's
  recorded throughput. Rendered with `~` and `est.`, e.g.
  `Transcribing (parakeet) ~2:23:12 (25% est.) · ETA 34m`.

`parakeet-cli` emits nothing until it exits (every token line of a 4-minute file arrives in
one burst at the end), which is why in-chunk positions can only ever be estimates. The
**ETA is derived only from measured checkpoints** and the interval between them; none is
shown until two exist. Throughput samples are kept in `D:\AI\Cache\stt_rate.json` (rolling
last 10) so estimates sharpen with use.

### Cancellation

`POST /api/v1/jobs/{id}/cancel` now stops the work, not just the bookkeeping:
- checked between chunks, and
- the engine process is polled once a second and terminated, so CPU is released within
  about a second rather than at the end of the recording.

A cancellation is distinct from a failure, so it never triggers the whisper fallback.

### Engine fallback

If parakeet fails, the job falls back to `faster-whisper base` on CPU and **says so** in
`speed`, including the expected slowdown for long audio. Previously this was silent, which
made a 9-hour fallback indistinguishable from a hang.

---

---

## ⚡ Workstation Constraints & Ergonomics

1. **Host P-Core Protection**:
   All background processing is locked to E-cores 8–15 with `taskset -c 8-15 nice -n 10`. Host Windows gaming and interactive tasks on P-cores 0–7 experience 0% stutter.
2. **Zero-Leak Policy**:
   All persistent and scratch files are written to `D:\` (`D:\Output\`, `D:\AI\Cache\`, `D:\AI\Vault\`). The Windows `C:\` drive is never touched.
3. **On-Demand Socket Activation**:
   Systemd socket (`media-api.socket`) listens on port 8080. When idle, the service consumes **0 MB RAM and 0% CPU**. The first incoming HTTP packet wakes the server in under 1 second.
4. **Automatic yt-dlp Self-Updating**:
   Runs background check on service startup and daily (`MEDIA_API_AUTO_UPDATE_YTDLP=true`), ensuring extractor definitions never rot against YouTube site changes.
5. **Automatic Authentication (`cookies.txt`)**:
   Dropping a Netscape-format `cookies.txt` into `D:\AI\Cache\cookies.txt` automatically authenticates all `yt-dlp` operations for private or age-restricted media.