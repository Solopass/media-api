from pathlib import Path
from typing import Dict, Any, Optional

from app.config import settings
from app.services.parakeet_service import ParakeetService
from app.services.whisper_service import WhisperService

ENGINES = ("parakeet", "whisper")


def resolve_engine(engine: Optional[str], language: Optional[str]) -> str:
    """Pick the engine for a job. Parakeet only covers 25 European languages; other languages use faster-whisper."""
    chosen = (engine or settings.stt_engine).lower()
    if chosen not in ENGINES:
        raise ValueError(f"Unknown STT engine '{chosen}' (expected one of {', '.join(ENGINES)})")
    if chosen == "parakeet" and not ParakeetService.supports(language):
        return "whisper"
    return chosen


def transcribe(
    audio_path: Path,
    engine: Optional[str] = None,
    model_size: str = "tiny",
    language: Optional[str] = None,
) -> Dict[str, Any]:
    chosen = resolve_engine(engine, language)
    if chosen == "parakeet":
        return ParakeetService.transcribe(audio_path, language=language)
    result = WhisperService.transcribe(audio_path, model_size=model_size, language=language)
    result.update({"engine": "whisper", "model": f"faster-whisper {model_size}"})
    return result
