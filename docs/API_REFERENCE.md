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
| `POST` | `/api/v1/ingest` | One-shot pipeline: Download → Clip → Extract Audio → STT → Ollama Summary → Obsidian Note |
| `POST` | `/api/v1/batch/expand-playlist` | Fast flat-extraction of YouTube playlists, channels, or video metadata for batch ingestion (<2s) |
| `POST` | `/api/v1/vault/resummarize` | Regenerate or change summary preset for existing Obsidian note in-place without re-transcribing |
| `POST` | `/api/v1/upload` | Stream uploaded media directly into `D:\AI\Cache\uploads\` |
| `GET` | `/api/v1/media/stream` | Stream audio/video with HTTP Range byte seeking support |
| `GET` | `/api/v1/vault/recent` | Recursively scan `D:\AI\Vault\Transcripts` for recent transcript notes |
| `GET` | `/api/v1/vault/note` | Retrieve note content, wikilinks, and Obsidian desktop URI |
| `GET` | `/api/v1/status/{job_id}` | Poll asynchronous job status, progress %, ETA, and output file paths |
| `GET` | `/api/v1/jobs` | View queue history of all recent workstation operations |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Immediately cancel a queued or running job |
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
  "summarize": true,
  "llm_model": "sol-fast",
  "summary_preset": "executive",
  "summary_focus": "key architectural decisions and performance metrics",
  "vault_subfolder": "Transcripts/Meetings",
  "tags": ["architecture", "review"]
}
```
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
Replaces the `> [!abstract]` callout in-place while preserving YAML frontmatter, headers, and timestamped transcripts.

### 7. `GET /api/v1/models` (GPU VRAM & Model Headroom)
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