# 📹 Workstation Linux Media API Reference

> REST API for automated YouTube downloads, lossless video clipping, speech-to-text transcription via `faster-whisper`, and direct Obsidian vault ingestion.

---

## ⚡ Quick Endpoints Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/health` | Health audit, core affinity & storage verification |
| `POST` | `/api/v1/download` | Download YouTube or web media to `D:\Output\Videos\` |
| `POST` | `/api/v1/clip` | Slice video/audio between start and end timestamps |
| `POST` | `/api/v1/transcribe` | Transcribe media via `faster-whisper` |
| `POST` | `/api/v1/ingest` | One-shot pipeline: Download → Clip → Transcribe → Ollama Summary → Obsidian Note |
| `GET` | `/api/v1/status/{id}` | Poll asynchronous job status, progress %, and files |
| `GET` | `/api/v1/jobs` | View queue of all recent operations |
| `POST` | `/api/v1/shutdown` | Gracefully terminate worker immediately to free system resources |

---

## ⚡ On-Demand Architecture & Zero-RAM Standby

This API follows the **Workstation On-Demand Standard**:
* **Zero Standby Footprint**: Systemd manages `media-api.socket` on port 8080. When idle, the Python worker is completely dormant (**0 MB RAM, 0% CPU**).
* **Instant Wakeup**: The moment any HTTP request or CLI helper is executed, systemd instantly spawns the worker.
* **Auto-Close Watchdog**: While jobs are running, the worker stays alive. Once the queue is empty and the server is idle for 60 seconds (or immediately via `--close` / `POST /api/v1/shutdown`), it terminates cleanly, returning all memory to the workstation.
* **Socket Re-Arming**: When the worker exits, systemd automatically re-arms the socket on standby.

---

## 💻 CLI Commands (PowerShell & WSL)

Both Windows PowerShell and WSL have unified on-demand CLI commands:

```powershell
# Download media (automatically wakes API, displays progress, auto-closes if requested)
media-download "https://www.youtube.com/watch?v=..." [-Format mp4] [-AudioOnly] [-NoWait] [-Close]

# Precision slice video/audio
media-clip "path/or/url" 00:01:00 00:02:30 [-Format mp4] [-ReEncode] [-Close]

# Speech-to-text transcription via faster-whisper on AVX-512
media-transcribe "path/or/url" [-Model tiny] [-Language en] [-Close]

# Full Ingest -> Ollama Summary -> Obsidian Vault Note
media-ingest "https://www.youtube.com/watch?v=..." [-Model tiny] [-NoSummary] [-Close]

# Inspect asynchronous job status
media-status "<job-id>"

# Immediate clean shutdown
media-shutdown [--Force]

# Inspect socket and service status
media-api [status | standby | start | stop | restart | logs]
```

---

## 🚀 REST Examples

### 1. Download Video
```bash
curl -X POST http://localhost:8080/api/v1/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "format": "mp4"}'
```

### 2. Extract Precision Clip
```bash
curl -X POST http://localhost:8080/api/v1/clip \
  -H "Content-Type: application/json" \
  -d '{
    "source": "/mnt/d/Output/Videos/sample.mp4",
    "start_time": "00:01:15",
    "end_time": "00:01:45",
    "format": "mp4"
  }'
```

### 3. One-Shot Obsidian Ingest Pipeline
```bash
curl -X POST http://localhost:8080/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "model_size": "tiny",
    "summarize": true,
    "llm_model": "qwen2.5-coder:7b"
  }'
```

### 4. Poll Job Status
```bash
curl http://localhost:8080/api/v1/status/YOUR_JOB_ID
```

### 5. Graceful Shutdown
```bash
curl -X POST http://localhost:8080/api/v1/shutdown \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```
\n