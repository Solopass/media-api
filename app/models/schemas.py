from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class MediaFormat(str, Enum):
    MP4 = "mp4"
    WEBM = "webm"
    MP3 = "mp3"
    WAV = "wav"
    M4A = "m4a"

class DownloadRequest(BaseModel):
    url: str = Field(..., description="Target video URL (YouTube, Podcast, etc.)")
    format: MediaFormat = Field(default=MediaFormat.MP4, description="Desired output format")
    audio_only: bool = Field(default=False, description="Extract audio only")
    priority: str = Field(default="normal", description="Queue priority: high, normal, idle")

class ClipRequest(BaseModel):
    source: str = Field(..., description="URL or absolute path to local video/audio file")
    start_time: str = Field(..., description="Start timestamp (e.g. HH:MM:SS or seconds)")
    end_time: str = Field(..., description="End timestamp (e.g. HH:MM:SS or seconds)")
    format: MediaFormat = Field(default=MediaFormat.MP4, description="Output format")
    re_encode: bool = Field(default=False, description="Force frame-accurate re-encode vs fast keyframe copy")

class TranscribeRequest(BaseModel):
    source: str = Field(..., description="URL or local media path to transcribe")
    engine: Optional[str] = Field(default=None, description="STT engine: parakeet or whisper (default from MEDIA_API_STT_ENGINE, parakeet)")
    model_size: str = Field(default="tiny", description="Whisper model size (whisper engine only): tiny, base, small, medium")
    language: Optional[str] = Field(default=None, description="Language code (e.g. en, es, zh) or auto-detect. Languages parakeet doesn't support use whisper")
    timestamps: bool = Field(default=True, description="Include word-level/segment timestamps")

class IngestRequest(BaseModel):
    source: str = Field(..., description="Video/Audio URL or local file path")
    clip_start: Optional[str] = Field(default=None, description="Optional clip start timecode")
    clip_end: Optional[str] = Field(default=None, description="Optional clip end timecode")
    engine: Optional[str] = Field(default=None, description="STT engine: parakeet or whisper (default from MEDIA_API_STT_ENGINE, parakeet)")
    model_size: str = Field(default="tiny", description="Whisper model size (whisper engine only)")
    summarize: bool = Field(default=True, description="Generate executive summary with local Ollama LLM")
    llm_model: str = Field(default="sol-fast", description="Local LLM model to query")
    priority: str = Field(default="normal", description="Queue priority")

class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str
    created_at: str

class JobDetail(BaseModel):
    job_id: str
    type: str
    source: str
    status: JobStatus
    progress_percent: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    output_files: Dict[str, str] = {}
    obsidian_link: Optional[str] = None
    obsidian_uri: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str

class ShutdownRequest(BaseModel):
    force: bool = Field(default=False, description="Force shutdown even if jobs are actively running")

class ShutdownResponse(BaseModel):
    message: str
    active_jobs: int
    scheduled: bool
