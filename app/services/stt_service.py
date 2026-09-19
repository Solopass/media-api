import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.services.ffmpeg_service import FFmpegService
from app.services.parakeet_service import ParakeetService
from app.services.whisper_service import WhisperService

logger = logging.getLogger("media_api")

ENGINES = ("parakeet", "whisper")

# parakeet-cli prints nothing until it exits (verified: every token line of a
# 4-minute file arrived in one burst at the end), so its progress cannot be
# streamed. Instead a heartbeat extrapolates from how fast this workstation has
# transcribed before, and the result is flagged as an estimate.
HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_REALTIME_FACTOR = 8.0
RATE_SAMPLES_KEPT = 10


def _rate_file() -> Path:
    return settings.cache_dir / "stt_rate.json"


def observed_realtime_factor() -> float:
    """Audio seconds transcribed per wall second, averaged over recent runs."""
    try:
        samples = json.loads(_rate_file().read_text(encoding="utf-8")).get("samples", [])
        usable = [float(s) for s in samples if float(s) > 0]
        if usable:
            return sum(usable) / len(usable)
    except Exception:
        pass
    return DEFAULT_REALTIME_FACTOR


def record_realtime_factor(audio_seconds: float, wall_seconds: float):
    """Remember how fast this run went, so the next estimate is better."""
    if audio_seconds <= 0 or wall_seconds <= 0:
        return
    factor = audio_seconds / wall_seconds
    try:
        path = _rate_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        samples = []
        if path.exists():
            samples = json.loads(path.read_text(encoding="utf-8")).get("samples", [])
        samples.append(round(factor, 3))
        path.write_text(
            json.dumps({"samples": samples[-RATE_SAMPLES_KEPT:]}), encoding="utf-8"
        )
        logger.info(f"[stt] Transcribed at {factor:.1f}x realtime")
    except Exception as e:
        logger.debug(f"[stt] Could not record rate: {e}")


class _Heartbeat:
    """Reports an estimated position while a silent engine works."""

    def __init__(self, audio_seconds: float, callback: Callable[..., None]):
        self.audio_seconds = audio_seconds
        self.callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at = 0.0

    def __enter__(self):
        if self.audio_seconds > 0 and self.callback:
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="stt-heartbeat", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        expected_total = self.audio_seconds / max(0.5, observed_realtime_factor())
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            elapsed = time.time() - self.started_at
            # Never claim completion: the real end is when the process exits.
            fraction = min(0.99, elapsed / expected_total) if expected_total > 0 else 0.0
            try:
                self.callback(fraction, fraction * self.audio_seconds, True)
            except Exception:
                pass

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        return False


def _audio_duration(audio_path: Path) -> float:
    try:
        return float(FFmpegService.probe_local_media(audio_path).get("duration") or 0.0)
    except Exception:
        return 0.0


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
    progress_callback: Optional[Callable[[float, float], None]] = None,
) -> Dict[str, Any]:
    """
    progress_callback(fraction, seconds_transcribed, estimated) is optional.

    Whisper reports real positions as it yields segments. Parakeet cannot, so a
    heartbeat supplies an estimate and marks it as one.
    """
    chosen = resolve_engine(engine, language)
    started = time.time()

    if chosen == "parakeet":
        audio_seconds = _audio_duration(audio_path) if progress_callback else 0.0
        try:
            with _Heartbeat(audio_seconds, progress_callback):
                result = ParakeetService.transcribe(audio_path, language=language)
            record_realtime_factor(float(result.get("duration") or audio_seconds), time.time() - started)
            return result
        except Exception as e:
            logger.warning(
                f"[stt] Parakeet transcription failed ({e}). Automatically falling back to faster-whisper (base)..."
            )
            result = WhisperService.transcribe(
                audio_path, model_size="base", language=language, progress_callback=progress_callback
            )
            result.update({"engine": "whisper (fallback)", "model": "faster-whisper base (fallback)"})
            return result

    result = WhisperService.transcribe(
        audio_path, model_size=model_size, language=language, progress_callback=progress_callback
    )
    result.update({"engine": "whisper", "model": f"faster-whisper {model_size}"})
    record_realtime_factor(float(result.get("duration") or 0.0), time.time() - started)
    return result
