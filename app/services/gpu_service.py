import time
import json
import logging
import subprocess
import urllib.request
from typing import Dict, Any, List, Optional

from app.config import settings

logger = logging.getLogger("media_api.gpu")

_VRAM_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "data": None
}

KNOWN_MODEL_VRAM_NEEDS = {
    "sol-vision": 7.5,
    "sol-vision:latest": 7.5,
    "gemma4:12b": 7.5,
    "sol-fast": 12.4,
    "sol-fast:latest": 12.4,
    "gpt-oss:20b": 12.4,
    "sol-specialist": 18.5,
    "sol-specialist:latest": 18.5,
    "qwen3.8:27b": 18.5,
    "sol-coder": 18.5,
    "sol-coder:latest": 18.5,
    "qwen3-coder:30b": 18.5,
    "sol-coder-next:latest": 52.0,
    "gpt-oss:120b": 65.0,
    "nomic-embed-text": 0.35,
    "nomic-embed-text:latest": 0.35,
}

class GpuService:
    @classmethod
    def get_vram_usage(cls, max_cache_age: float = 6.0) -> Dict[str, Any]:
        """Fetch current dedicated VRAM usage on the discrete GPU (cached for 6s)."""
        now = time.time()
        if _VRAM_CACHE["data"] and (now - _VRAM_CACHE["timestamp"]) < max_cache_age:
            return _VRAM_CACHE["data"]

        card_gb = settings.gpu_total_vram_gb
        used_gb = 4.0  # Safe default baseline if counter fails
        try:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$m = ((Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | Measure-Object CookedValue -Maximum).Maximum; if ($m) { [math]::Round($m / 1GB, 2) } else { -1 }"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.strip())
                if val > 0:
                    used_gb = val
        except Exception as e:
            logger.debug(f"VRAM counter query fallback used: {e}")

        # Check what Ollama currently has loaded
        ollama_loaded_vram = 0.0
        loaded_model_names = []
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    loaded_model_names.append(m.get("name"))
                    ollama_loaded_vram += float(m.get("size_vram", 0)) / (1024**3)
        except Exception:
            pass

        # Room for a new model assumes existing Ollama models can be unloaded
        other_apps_gb = max(0.0, round(used_gb - ollama_loaded_vram, 2))
        free_for_model_gb = max(0.0, round(card_gb - other_apps_gb, 2))
        current_free_gb = max(0.0, round(card_gb - used_gb, 2))

        result = {
            "adapter": settings.gpu_name,
            "total_vram_gb": card_gb,
            "used_vram_gb": round(used_gb, 2),
            "free_vram_gb": current_free_gb,
            "other_apps_vram_gb": other_apps_gb,
            "room_for_model_gb": free_for_model_gb,
            "loaded_models": loaded_model_names
        }

        _VRAM_CACHE["timestamp"] = now
        _VRAM_CACHE["data"] = result
        return result

    @classmethod
    def estimate_vram_needed(cls, model_name: str, size_bytes: Optional[int] = None) -> float:
        """Estimate VRAM footprint in GB for a given model."""
        clean_name = model_name.split(":")[0]
        if model_name in KNOWN_MODEL_VRAM_NEEDS:
            return KNOWN_MODEL_VRAM_NEEDS[model_name]
        if clean_name in KNOWN_MODEL_VRAM_NEEDS:
            return KNOWN_MODEL_VRAM_NEEDS[clean_name]

        if size_bytes and size_bytes > 0:
            # Model weights + KV cache & compute buffer allowance
            weights_gb = size_bytes / (1024**3)
            return round(weights_gb * 1.05 + 0.8, 1)

        return 12.4

    @classmethod
    def evaluate_model_fit(cls, model_name: str, size_bytes: Optional[int] = None, vram_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Determine whether a model fits cleanly into GPU VRAM or will cause PCIe eviction."""
        vram = vram_info or cls.get_vram_usage()
        needed_gb = cls.estimate_vram_needed(model_name, size_bytes)
        room_gb = vram["room_for_model_gb"]
        margin_gb = round(room_gb - needed_gb, 2)

        if margin_gb >= 0.8:
            verdict = "fits"
            badge = f"🟢 Fits easily (+{margin_gb:.1f} GB headroom)"
            short_label = f"Fits (+{margin_gb:.1f} GB)"
            note = f"100% on GPU. Zero PCIe bottleneck (needs ~{needed_gb} GB, {room_gb:.1f} GB available)."
        elif margin_gb >= 0.0:
            verdict = "tight"
            badge = f"🟡 Fits, but tight ({margin_gb:.1f} GB spare)"
            short_label = f"Tight (+{margin_gb:.1f} GB)"
            note = f"Fits now, but opening more browser tabs/video may push it into system RAM."
        elif margin_gb >= -1.2:
            verdict = "tight"
            badge = f"🟡 Tight ({abs(margin_gb):.1f} GB short — close 1 browser)"
            short_label = f"Tight ({abs(margin_gb):.1f} GB short)"
            note = f"Needs ~{needed_gb} GB, room is {room_gb:.1f} GB. Closing one browser/Discord will give full speed."
        else:
            verdict = "wont_fit"
            badge = f"🔴 Exceeds VRAM ({abs(margin_gb):.1f} GB short — slow)"
            short_label = f"Exceeds VRAM ({abs(margin_gb):.1f} GB short)"
            note = f"Model is ~{needed_gb} GB. Requires CPU offload over PCIe, which will slow down summaries."

        return {
            "model_name": model_name,
            "vram_needed_gb": needed_gb,
            "room_gb": room_gb,
            "margin_gb": margin_gb,
            "verdict": verdict,
            "badge": badge,
            "short_label": short_label,
            "note": note
        }
