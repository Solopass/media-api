# YouTube Local API

A lightweight, local PowerShell API server to download and clip YouTube videos using `yt-dlp` and `ffmpeg`.

## Features
- **Async Processing:** Background threads handle downloads and clipping without blocking the API.
- **Timestamp Clipping:** Grabs a specific portion of a video (e.g., 00:01:00 to 00:01:30) via direct stream, avoiding full downloads.
- **Multiple Formats:** Supports `mp4`, `mp3`, `wav`, `mkv`, and `webm`.
- **Batch Processing:** Submit multiple jobs at once via a JSON POST request.

## Prerequisites
- PowerShell Core (`pwsh`)
- `yt-dlp`
- `ffmpeg`

## Usage

### 1. Starting the Server (Manual Mode)
If you only want the server running when you need it:
```powershell
pwsh .\YouTubeAPI.ps1 -Port 8080
```
*To stop the server, simply press `Ctrl+C` or close the terminal window.*

### 2. Starting the Server (Background Service Mode)
If you want to install it as a Windows service that you can easily turn on and off in the background:
```powershell
# Install the service (Requires nssm to be installed via winget)
pwsh .\Install-Service.ps1 -Port 8080

# Turn the service ON
nssm start YouTubeLocalAPI

# Turn the service OFF
nssm stop YouTubeLocalAPI
```

## Endpoints

### `/download` (GET)
Downloads a full video or audio track.
- `url`: The YouTube URL (Required)
- `format`: `mp4`, `mp3`, `wav`, `mkv`, `webm` (Default: `mp4`)
*Example: `http://localhost:8080/download?url=https://youtu.be/...&format=mp3`*

### `/clip` (GET)
Extracts a specific timestamped clip.
- `url`: The YouTube URL (Required)
- `start`: Start time in `HH:MM:SS` (Required)
- `end`: End time in `HH:MM:SS` (Required)
- `format`: `mp4`, `mp3` (Default: `mp4`)
*Example: `http://localhost:8080/clip?url=https://youtu.be/...&start=00:00:00&end=00:00:10`*

### `/status` (GET)
Checks the progress of a job returned by `/download` or `/clip`.
- `id`: The Job ID (Required)
*Example: `http://localhost:8080/status?id=12345-abcde`*

### `/batch` (POST)
Submit multiple clips/downloads in one request.
Body format:
```json
[
  { "url": "...", "start": "00:01:00", "end": "00:02:00", "format": "mp4" },
  { "url": "...", "format": "mp3" }
]
```

### `/metrics` (GET)
Returns uptime, total requests, and active jobs.
