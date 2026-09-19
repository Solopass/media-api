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
    normalize_audio: bool = Field(default=False, description="Apply EBU R128 loudness normalization")
    speech_optimized: bool = Field(default=False, description="Convert to 16kHz mono WAV for speech/STT")

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
    language: Optional[str] = Field(default=None, description="Language code (e.g. en, es, zh) or null to auto-detect. Languages parakeet does not cover fall back to whisper")
    summarize: bool = Field(default=True, description="Generate executive summary with local Ollama LLM")
    llm_model: str = Field(default="sol-fast", description="Local LLM model to query")
    summary_preset: str = Field(default="executive", description="Summary template: executive, meeting, timeline, technical, bullets")
    summary_focus: Optional[str] = Field(default=None, description="Optional custom user focus instructions")
    vault_subfolder: str = Field(default="Transcripts", description="Subfolder path inside D:\\AI\\Vault")
    tags: list[str] = Field(default_factory=list, description="Custom tags for Obsidian YAML frontmatter")
    keep_video: bool = Field(default=False, description="Keep the downloaded source video after transcription (default: delete it; extracted audio and transcript are always kept)")
    generate_chapters: bool = Field(default=False, description="Also generate YouTube-style chapter markers (a second local LLM pass)")
    priority: str = Field(default="normal", description="Queue priority")

class ConfigIdleRequest(BaseModel):
    timeout_seconds: int = Field(default=300, description="Idle timeout in seconds (0 or negative = disabled)")
    auto_close: bool = Field(default=True, description="Enable automatic shutdown when idle")

class MediaProbeResponse(BaseModel):
    title: str
    uploader: str
    duration: float = 0.0
    duration_formatted: str = "00:00"
    thumbnail: Optional[str] = None
    description: Optional[str] = None

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

class ResummarizeRequest(BaseModel):
    note_path: str
    llm_model: str = "sol-fast"
    summary_preset: str = "executive"
    summary_focus: Optional[str] = None

class ResummarizeResponse(BaseModel):
    success: bool
    note_path: str
    summary: str
    model: str
    preset: str

class PlaylistItem(BaseModel):
    title: str
    url: str
    duration: float = 0.0
    duration_formatted: str = "00:00"
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None

class PlaylistExpandRequest(BaseModel):
    url: str = Field(..., description="YouTube playlist, channel, or video URL")
    max_items: int = Field(default=50, ge=1, le=500, description="Maximum number of items to extract")

class PlaylistExpandResponse(BaseModel):
    title: str
    uploader: Optional[str] = None
    total_items: int
    items: List[PlaylistItem]


class StorageCategory(BaseModel):
    name: str
    label: str
    directory: str
    windows_directory: str
    file_count: int
    total_bytes: int
    total_mb: float
    oldest_modified: Optional[str] = None
    newest_modified: Optional[str] = None
    reclaimable: bool


class StorageDrive(BaseModel):
    path: str
    total_gb: float
    used_gb: float
    free_gb: float


class StorageAgeBucket(BaseModel):
    days: int
    file_count: int
    bytes: int
    mb: float


class StorageResponse(BaseModel):
    drive: StorageDrive
    categories: List[StorageCategory]
    total_bytes: int
    total_mb: float
    reclaimable_by_age: Dict[str, StorageAgeBucket]
    scanned_at: str


class StorageCandidate(BaseModel):
    path: str
    windows_path: str
    category: str
    size_bytes: int
    size_mb: float
    modified_at: str


class StorageReclaimRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=0, le=3650, description="Only files last modified more than this many days ago")
    categories: List[str] = Field(default=["videos", "audio", "uploads"], description="Reclaimable categories: videos, audio, uploads. Transcript sidecars and notes are never deleted")
    dry_run: bool = Field(default=True, description="Preview the candidates without deleting anything")


class StorageReclaimResponse(BaseModel):
    dry_run: bool
    older_than_days: int
    categories: List[str]
    candidates: List[StorageCandidate]
    file_count: int
    total_bytes: int
    total_mb: float
    freed_bytes: int = 0
    freed_mb: float = 0.0
    errors: List[str] = []


class Chapter(BaseModel):
    start: float
    timestamp: str
    title: str


class ChaptersRequest(BaseModel):
    note_path: str = Field(..., description="Path or filename of an existing transcript note")
    llm_model: str = Field(default="sol-fast", description="Local LLM model to query")


class ChaptersResponse(BaseModel):
    success: bool
    note_path: str
    chapters: List[Chapter]
    youtube_block: str
    model: str
