# YouTube Local API - Agent Reference

This document provides instructions for AI agents and users on how to manage and interact with the local YouTube API service.

## Service Management (On/Off)

The API should only be turned on when needed to save system resources. You have two options for running it:

### 1. Manual Execution (Recommended for temporary usage)
To turn the service **ON**:
```powershell
# Run this in a background task or separate terminal
pwsh .\YouTubeAPI.ps1 -Port 8080
```
To turn the service **OFF**:
Terminate the PowerShell process running the script.

### 2. Windows Service Mode (via nssm)
If the user installed the service via `.\Install-Service.ps1`, you can manage it using `nssm`:
- Turn **ON**: `nssm start YouTubeLocalAPI`
- Turn **OFF**: `nssm stop YouTubeLocalAPI`
- Check Status: `nssm status YouTubeLocalAPI`

## Making Requests to the API

All endpoints return JSON. Downloads and clips are processed **asynchronously**, meaning you will immediately receive a `JobId` that you must poll using the `/status` endpoint to get the final file path.

### 1. Requesting a Clip
**Endpoint:** `GET http://localhost:8080/clip?url={URL}&start={HH:MM:SS}&end={HH:MM:SS}`
**Response:**
```json
{
  "Id": "09a547fc-1c84-4aa6-ad88-e216975e1c7e",
  "Status": "queued"
}
```

### 2. Requesting a Full Download
**Endpoint:** `GET http://localhost:8080/download?url={URL}&format={mp4|mp3|wav}`
**Response:** Same as above (Job object).

### 3. Polling Job Status
**Endpoint:** `GET http://localhost:8080/status?id={JobId}`
**Behavior:** Poll this endpoint (e.g., every 2-5 seconds). When `"Status": "completed"`, the `"File"` field will contain the absolute path to the downloaded media.

```json
{
  "Id": "09a547fc-1c84-4aa6-ad88-e216975e1c7e",
  "Status": "completed",
  "File": "C:\\Users\\...\\downloads\\clip_00-00-00_00-00-10_abc.mp4",
  "Error": ""
}
```

### 4. Batch Operations
**Endpoint:** `POST http://localhost:8080/batch`
**Body:** Array of job requests.
```json
[
  { "url": "https://youtu.be/...", "start": "00:00:00", "end": "00:00:10", "format": "mp4" }
]
```

## Troubleshooting
- If requests fail, ensure the service is running on the correct port (default 8080).
- Logs are written to `YouTubeAPI.log` in the same directory.
- `yt-dlp` and `ffmpeg` must be accessible in the system PATH.
