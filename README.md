# 🎙️ SOL Media Studio (`media-api`)

> High-performance, on-demand media workstation suite running on `127.0.0.1:8080` in WSL `Ubuntu-24.04` on Windows 11. Provides automated web media downloading, frame-accurate clipping, loudness normalization, multi-engine speech-to-text transcription, AI briefing generation via local LLMs, and direct Obsidian Second Brain vault ingestion.

---

## 🌟 Key Capabilities

### 1. One-Shot Obsidian Ingest Pipeline
- **Unified Media Input**: Seamlessly accepts YouTube/web URLs, local Windows paths (`D:\...`), WSL paths (`/mnt/d/...`), or direct drag-and-drop file uploads.
- **Optional Trimming**: Slices input before speech recognition to extract only relevant intervals.
- **5 Structured AI Summary Presets**:
  - `executive`: Core Briefing, Key Themes & Decisions, Deliverables & Action Items.
  - `meeting`: Attendees, Agenda Topics, Decisions Reached, Action Item Ownership Table (`| Owner | Action Item | Target |`).
  - `timeline`: Chronological Topic Breakdown & Timeline tracking major topic shifts.
  - `technical`: Architectural & System Overview, APIs/Schemas, Technical Trade-offs, Implementation Notes.
  - `bullets`: Dense, high-signal bulleted takeaways with bold lead-ins.
- **Topical Focus Guidance**: Inject custom user instructions into the local LLM prompt (`summary_focus`).
- **Automatic Source Video Cleanup**: The video a job produced (download, clip intermediate, or upload-cache copy) is deleted once the vault note is written, so `D:\Output\Videos` does not grow unbounded — cancelled and failed runs reclaim theirs too. Flip **Keep Source Video** in the UI (or `"keep_video": true`) to retain it. Extracted audio and the `.transcript.json` sidecar are always kept for the in-app player, and a file the job did not produce is never deleted, so re-ingesting one of your own videos by path leaves it alone.
- **Automatic Chapter Markers**: Flip **Generate Chapters** to add YouTube-style markers (`00:00 Intro`, `03:45 Architecture`) to the note — as a `chapters:` frontmatter list *and* a readable `## 🎬 Chapters` section — or add them to any existing note from the Vault Explorer. Chapters are click-to-seek in the in-app player and copy out as a paste-ready description block. The model is fed a timestamped digest sampled across the whole recording, and every timestamp it returns is validated against the real duration, so a bad generation yields no chapters rather than wrong ones.
- **Per-Job Language Override**: Leave **Language** empty to auto-detect, or pass a code (`en`, `es`, `ja`). Parakeet covers 25 European languages; anything else routes to faster-whisper automatically.
- **Obsidian Second Brain Integration**: Automatically creates formatted notes in `D:\AI\Vault\Transcripts/` with YAML frontmatter, summary callouts (`> [!abstract]`), timestamped transcript tables, deep-links (`obsidian://open?...`), and non-blocking semantic vector re-indexing (`find-concept --reindex`).

### 2. Audio Engineering & Interactive Playback
- **Follow-Playhead Transcript Auto-Scroll**: Highlights the currently playing sentence and scrolls it smoothly into view; automatically pauses (`📜 Follow: PAUSED`) on manual scroll to prevent fighting the user.
- **Precision A-B Audio Looping**: Set `[Set A]`, `[Set B]`, `[🔁 Loop A-B]`, and `[✕ Clear]` with live interval pill badge for seamless repetitive listening.
- **Speed & Scrubbing Controls**: `0.75x`, `1.0x`, `1.25x`, `1.5x`, `2.0x` toggles, `-5s`/`+5s` buttons, per-sentence replay button (`↺`), and global keyboard shortcuts (`Space`, `J`/`L`, `[`/`]`, `Ctrl+F`).
- **Broadcast-Standard Subtitle Exports**: Instant client-side download for `.SRT`, `.VTT`, and `.JSON` formats with EBU-compliant 42-character line wrapping.
- **Volume Normalization & 16kHz WAV**: Built-in dual-pass EBU R128 loudness filter (`loudnorm`) and 16kHz mono WAV downsampler for fast speech inference.

### 3. Multi-URL & Playlist Batch Queue
- **Mode Switcher**: Easily toggle between single-target ingestion and multi-URL batch mode.
- **YouTube Playlist & Channel Auto-Expansion**: Sub-second flat metadata extraction resolves playlists or channels into individual video items without downloading media upfront.
- **Playlist URLs Anywhere**: Paste a playlist or channel URL into the single-ingest field and the Studio switches to Batch mode and resolves it, rather than trying to download the collection as one video. Private, deleted and region-blocked entries are dropped, with the count shown ("Resolved Items — 10 unavailable skipped") so the total adding up to less than the playlist length is never a mystery.
- **Interactive Pre-flight Checklist**: Review video thumbnails, titles, and durations before launching, with selective checkbox toggles and "Select / Deselect All" helpers.
- **Sequential Pipeline Queue**: Ingest and transcription jobs run on a single server-side worker thread, one at a time, so a batch (or a second browser tab) cannot cause local LLM / Ollama GPU memory collisions. Host gaming stays smooth.
- **Per-Item Obsidian Deep-Links**: Live progress indicators update in real time; each finished item row immediately provides an **Open Note** button and wikilink badge (`[[Transcripts/...]]`).

### 4. GPU VRAM & Memory Footprint Awareness
- **Real-Time Adapter VRAM Telemetry**: Queries dedicated adapter VRAM (e.g. AMD Radeon RX 9070 XT 16GB) to prevent model-induced system paging.
- **Dynamic Model Headroom Badges**: Color-coded badges next to the model selector inform the user if a model fits with safe headroom (`Fits VRAM`), runs close to the limit (`Tight`), or exceeds dedicated VRAM (`Exceeds VRAM`).
- **In-Place Re-Summarization**: Switch summary presets (`executive`, `meeting`, `timeline`, `technical`, `bullets`) or change models for existing vault notes in-place without re-transcribing.

### 5. Queue Management, Auto-Updates & Vault Explorer
- **URL Metadata Pre-fetch**: Sub-second video card preview (title, uploader, duration, thumbnail) on typing or pasting a URL.
- **Automatic `yt-dlp` Self-Updating**: Background checks on startup and daily ensure extractor definitions never fail against YouTube website updates.
- **Chunked Transcription for Long Audio**: Recordings past 20 minutes are split on silence and transcribed piece by piece. `parakeet-cli` cannot decode a long file at all — a 9h05m audiobook exits 5 while decoding at any sample rate, while 4h decodes fine — so chunking is what makes multi-hour media work. It also means real per-chunk progress, cancellation that takes effect in about a second, and a failure that costs one chunk instead of the whole book.
- **Live Stage Progress**: Long stages report continuously instead of sitting on one number for an hour. ffmpeg extraction streams its real position; transcription shows a calibrated estimate (marked `~` and `est.`) because `parakeet-cli` prints nothing until it exits. The estimate learns from every run — the workstation's measured throughput is stored and averaged, so ETAs sharpen over time.
- **Live Job Monitor**: Real-time task progress, speeds, cancellation (`✕ Cancel`), and history purging (`Clear Finished`).
- **Vault Explorer Tab**: Recursively scans recent markdown transcripts in `D:\AI\Vault\Transcripts`, displays tags and metadata, copies wikilinks with one click, and offers an embedded preview drawer.
- **Dynamic Idle Shutdown**: Configure auto-sleep directly from the header (`1m`, `5m`, `15m`, `30m`, `Keep Awake`).
- **Durable Error Logging**: Everything the server logs is written to `D:\AI\Cache\logs\media-api.log` (rotating, 2 MB x 3), not just stdout, so a failure survives the WSL VM being torn down. Browser-side failures are reported too — and when the server is unreachable at that moment, they are buffered in the page and flushed once it answers again, which is what makes a mid-batch death visible afterwards. `GET /api/v1/diagnostics` returns recent warnings and errors.
- **Storage Breakdown & Reclaim**: Click the **D: Drive** chip in the header for a per-category breakdown of `D:\Output` and the upload cache, then reclaim media older than 7/30/90 days. Always a two-step flow — preview the exact file list, then confirm — and transcript sidecars, notes, and any file belonging to a running job are never deleted.

---

## 🛡️ Workstation Architecture & Constraints

- **Host P-Core Protection**: Background workers are pinned to E-cores 8–15 (`taskset -c 8-15 nice -n 10`), leaving P-cores 0–7 100% free for host Windows responsiveness.
- **Zero-Leak Storage Policy**: All writes stay strictly on `D:\` (`D:\Output`, `D:\AI\Cache`, `D:\AI\Vault`). The Windows `C:\` drive is never touched.
- **Allow-Listed File Access**: Every endpoint that takes a caller-supplied path (`/media/stream`, `/vault/note`, `/vault/resummarize`) resolves it against a fixed root list in `app/paths.py`. The AI cache root is deliberately excluded — only `Cache\uploads` is reachable — so `cookies.txt`, agent history and browser profiles are not served.
- **On-Demand Socket Activation**: Managed by systemd (`media-api.socket` on `127.0.0.1:8080`). Consumes **0 MB RAM and 0% CPU** on standby; wakes in <1s on the first HTTP request — **but only while the WSL distro is running**. A stopped distro cannot be woken by an HTTP request from Windows, so the browser will simply report "Offline / Idle" forever. Always enter through the **SOL Media Studio** shortcut (`Media-Studio-Silent.vbs`), which holds the distro open with `wsl.exe -d Ubuntu-24.04 -- sleep infinity` for as long as it runs. Opening the UI from a bookmark after that holder has exited will not start anything.
- **Loopback-Only Binding**: Both socket activation and direct `./run.sh` invocation bind `127.0.0.1` only, so the API is never exposed to the network. To reach it from another machine deliberately, run `MEDIA_API_BIND_HOST=0.0.0.0 ./run.sh` (there is no authentication, so only do this on a trusted network).
- **Zero-Touch Isolation**: `sol-hud` remains completely untouched.

---

## 🚀 How to Launch & Use

### Desktop Shortcut (Recommended)
Double-click the **SOL Media Studio** shortcut on your Windows Desktop (or run `Media-Studio-Silent.vbs`).
- Launches the studio silently with **zero pop-up black console windows**.
- Opens your default web browser to `http://127.0.0.1:8080/`.

### Alternative Launch Options
- **Persistent Terminal**: Run `Media-Studio.bat` if you prefer an open command prompt window.
- **Recovering a dead Studio**: If the header shows "Offline / Idle" and refreshing does nothing, the distro has stopped. Re-run the shortcut — no amount of reloading the page will bring it back, because the listening socket lives inside the stopped distro.
- **Clean Shutdown**: Click the red **Shutdown** button in the top-right header of the web studio, or run `Stop-Media-Studio.bat`.

---

## 🔮 Potential Next Steps & Future Roadmap

When testing tomorrow, consider exploring these potential enhancements:

1. **Speaker Diarization (Multi-Speaker Labeling)**:
   - Integrate PyAnnote / WhisperX to identify distinct speakers (e.g. `Speaker A`, `Speaker B`) in transcript tables and exports.
2. ~~**Automated YouTube Video Chapters**~~ — shipped; see **Automatic Chapter Markers** above.
3. **Live Streaming STT (WebSocket)**:
   - Provide real-time microphone dictation directly into the Studio and Obsidian.
4. **PDF / Document Report Export**:
   - Generate styled executive briefing PDF summaries alongside `.SRT` and `.VTT`.
5. **Clipper Batch Queue**:
   - Expand the multi-file batch tray into the Media Clipper tab for bulk slicing.

---

## License

**Source-available, noncommercial.** Copyright © 2026 Solopass. Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

- ✅ **Free** for personal use, hobby projects, study and research, and for nonprofits, schools and public institutions.
- 💼 **Commercial use** (in a business, product or paid service, or for-profit internal use) needs a paid license. See [COMMERCIAL.md](COMMERCIAL.md), or contact [realsolopass@gmail.com](mailto:realsolopass@gmail.com) · <https://polymatica.pages.dev>.
