import os
from pathlib import Path
from pydantic import BaseModel

class Settings(BaseModel):
    # API Server Configuration
    app_name: str = "Workstation Linux Media API"
    app_version: str = "1.0.0"
    port: int = 8080
    host: str = "127.0.0.1"
    cors_origins: list[str] = ["http://127.0.0.1:8080", "http://localhost:8080"]

    # Storage Paths (Strictly on D:\ per workstation rules)
    workspace_dir: Path = Path("/mnt/d/Workspace/media-api")
    output_videos: Path = Path("/mnt/d/Output/Videos")
    output_audio: Path = Path("/mnt/d/Output/Audio")
    output_transcripts: Path = Path("/mnt/d/Output/Transcripts")
    
    # Obsidian Second Brain Paths
    vault_transcripts: Path = Path("/mnt/d/AI/Vault/Transcripts")
    vault_dashboard: Path = Path("/mnt/d/AI/Vault/Transcripts_Dashboard.md")
    vault_schedule: Path = Path("/mnt/d/AI/Vault/VM_Schedule.md")
    
    # AI & Model Caches (Strict 0-leak)
    cache_dir: Path = Path("/mnt/d/AI/Cache")
    uploads_dir: Path = Path("/mnt/d/AI/Cache/uploads")
    hf_cache: Path = Path("/mnt/d/AI/Cache/huggingface")
    whisper_models_dir: Path = Path("/mnt/d/AI/Models/Whisper")
    
    # Local LLM Inference
    ollama_api_url: str = "http://127.0.0.1:11434/api/chat"
    default_llm_model: str = "sol-fast"
    default_whisper_model: str = "tiny"  # tiny/base/small/medium

    # Speech-to-text engine: "parakeet" (default, most accurate in D:\OBVLT\reports\stt-bench.md) or "whisper".
    # parakeet-cli is a Windows binary run through WSL interop, so run.sh's E-core pinning does not apply to it;
    # parakeet_threads keeps it from taking every core.
    stt_engine: str = os.getenv("MEDIA_API_STT_ENGINE", "parakeet")
    parakeet_cli: Path = Path("/mnt/d/AI/bin/whisper-b5130-blas/Release/parakeet-cli.exe")
    parakeet_model: Path = Path("/mnt/d/AI/Models/Whisper/ggml-parakeet-tdt-0.6b-v3-q8_0.bin")
    parakeet_threads: int = int(os.getenv("MEDIA_API_PARAKEET_THREADS", "8"))

    # On-Demand Lifecycle & Auto-Close (Workstation Standard)
    idle_timeout_seconds: int = int(os.getenv("MEDIA_API_IDLE_TIMEOUT", "60"))
    auto_close: bool = True

    def setup_directories(self):
        for path in [
            self.output_videos,
            self.output_audio,
            self.output_transcripts,
            self.vault_transcripts,
            self.uploads_dir,
            self.hf_cache,
            self.whisper_models_dir
        ]:
            path.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(self.hf_cache)

def normalize_path(path_input: str | Path) -> Path:
    r"""Seamlessly normalize Windows (D:\...) and WSL (/mnt/d/...) file paths."""
    import re
    s = str(path_input).strip().strip('"').strip("'")
    if re.match(r"^[a-zA-Z]:[\\/]", s):
        drive = s[0].lower()
        rest = s[2:].replace("\\", "/").lstrip("/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(s)

def to_windows_path(path_input: str | Path) -> str:
    r"""Convert WSL (/mnt/d/...) path to Windows (D:\...) path."""
    s = str(path_input).strip().strip('"').strip("'")
    if s.startswith("/mnt/") and len(s) > 6 and s[6] == "/":
        drive = s[5].upper()
        rest = s[7:].replace("/", "\\")
        return f"{drive}:\\{rest}"
    return s

settings = Settings()
settings.setup_directories()

