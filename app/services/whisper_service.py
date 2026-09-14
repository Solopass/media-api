import os
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from faster_whisper import WhisperModel

from app.config import settings

class WhisperService:
    _instance = None
    _lock = threading.Lock()
    _inference_semaphore = threading.Semaphore(1)
    _models: Dict[str, WhisperModel] = {}

    @classmethod
    def get_model(cls, model_size: str = "tiny") -> WhisperModel:
        with cls._lock:
            if model_size not in cls._models:
                # Pin to CPU with AVX-512 (compute_type float32 or int8)
                model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    download_root=str(settings.whisper_models_dir)
                )
                cls._models[model_size] = model
            return cls._models[model_size]

    @classmethod
    def transcribe(
        cls,
        audio_path: Path,
        model_size: str = "tiny",
        language: Optional[str] = None
    ) -> Dict[str, Any]:
        model = cls.get_model(model_size)
        with cls._inference_semaphore:
            segments, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=5,
                vad_filter=True
            )
            # Materialize segments inside the lock to ensure streaming completes safely
            seg_list = []
            full_text_parts = []
            for s in segments:
                seg_list.append({
                    "id": s.id,
                    "start": round(s.start, 2),
                    "end": round(s.end, 2),
                    "text": s.text.strip()
                })
                full_text_parts.append(s.text.strip())

        full_text = " ".join(full_text_parts)
        return {
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "segments": seg_list,
            "full_text": full_text
        }
