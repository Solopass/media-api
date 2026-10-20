import os
import sys
import time
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from app.config import settings

logger = logging.getLogger("media_api.updater")

class UpdaterService:
    @staticmethod
    def get_timestamp_file() -> Path:
        return settings.cache_dir / "last_ytdlp_update.txt"

    @staticmethod
    def get_installed_version() -> str:
        try:
            import yt_dlp.version
            return getattr(yt_dlp.version, "__version__", "unknown")
        except Exception:
            return "unknown"

    @classmethod
    def get_last_update_time(cls) -> Optional[float]:
        ts_file = cls.get_timestamp_file()
        if not ts_file.exists():
            return None
        try:
            return float(ts_file.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    @classmethod
    def should_update(cls) -> bool:
        if not settings.auto_update_ytdlp:
            return False
        last_ts = cls.get_last_update_time()
        if last_ts is None:
            return True
        return (time.time() - last_ts) >= settings.ytdlp_update_interval_seconds

    @classmethod
    def record_update_timestamp(cls):
        try:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            cls.get_timestamp_file().write_text(str(time.time()), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[updater] Failed to record update timestamp: {e}")

    @classmethod
    async def run_update(cls, force: bool = False) -> Dict[str, Any]:
        prev_ver = cls.get_installed_version()

        if not force and not cls.should_update():
            last_ts = cls.get_last_update_time()
            elapsed = int(time.time() - last_ts) if last_ts else 0
            return {
                "status": "skipped",
                "message": f"Checked recently ({elapsed}s ago, interval is {settings.ytdlp_update_interval_seconds}s)",
                "version": prev_ver,
                "previous_version": prev_ver
            }

        logger.info(f"[updater] Starting yt-dlp update check (current version: {prev_ver})...")

        # Locate python & package manager (prefer uv for <200ms resolution)
        venv_python = settings.workspace_dir / ".venv" / "bin" / "python"
        python_bin = str(venv_python) if venv_python.exists() else sys.executable

        uv_bin = shutil.which("uv") or "/usr/local/bin/uv"
        if os.path.exists(uv_bin) and venv_python.exists():
            cmd = [uv_bin, "pip", "install", "--python", python_bin, "-U", "yt-dlp", "yt-dlp-ejs"]
        else:
            cmd = [python_bin, "-m", "pip", "install", "-U", "yt-dlp", "yt-dlp-ejs"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=45.0)
            stdout = stdout_data.decode("utf-8", errors="replace").strip()
            stderr = stderr_data.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                cls.record_update_timestamp()
            else:
                logger.warning(f"[updater] yt-dlp update command returned code {proc.returncode}: {stderr}")
                return {
                    "status": "error",
                    "error": stderr or f"Process returned code {proc.returncode}",
                    "version": prev_ver,
                    "previous_version": prev_ver
                }

            # Query updated version via python subprocess to avoid stale in-process module cache
            new_ver = prev_ver
            try:
                chk_proc = await asyncio.create_subprocess_exec(
                    python_bin, "-c", "import yt_dlp.version; print(yt_dlp.version.__version__)",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                chk_out, _ = await asyncio.wait_for(chk_proc.communicate(), timeout=5.0)
                if chk_proc.returncode == 0 and chk_out:
                    new_ver = chk_out.decode().strip()
            except Exception:
                pass

            if new_ver != prev_ver:
                logger.info(f"[updater] yt-dlp updated successfully from {prev_ver} -> {new_ver}!")
            else:
                logger.info(f"[updater] yt-dlp is already up to date ({new_ver}).")

            return {
                "status": "success",
                "version": new_ver,
                "previous_version": prev_ver,
                "updated": new_ver != prev_ver,
                "output": stdout
            }

        except asyncio.TimeoutError:
            logger.warning("[updater] yt-dlp update timed out after 45s (network slow or offline).")
            return {
                "status": "timeout",
                "error": "Update timed out",
                "version": prev_ver,
                "previous_version": prev_ver
            }
        except Exception as e:
            logger.warning(f"[updater] yt-dlp update failed: {e}")
            return {
                "status": "error",
                "error": str(e),
                "version": prev_ver,
                "previous_version": prev_ver
            }

    @classmethod
    async def background_auto_update(cls):
        """Non-blocking background check called during application startup."""
        try:
            # Wait 2 seconds so server socket activation and initial request can proceed instantly
            await asyncio.sleep(2)
            if cls.should_update():
                await cls.run_update(force=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[updater] Background update task encountered error: {e}")
