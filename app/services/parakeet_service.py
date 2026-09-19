import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.services.errors import TranscriptionCancelled

# Languages parakeet-tdt-0.6b-v3 was trained on. Anything else goes to faster-whisper.
PARAKEET_LANGUAGES = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
}
# Formats parakeet-cli decodes itself; everything else is converted to 16 kHz mono wav first.
NATIVE_FORMATS = {".wav", ".mp3", ".flac", ".ogg"}

# Token line printed to stderr with -ps, e.g.
#   [ 4] id=  366 frame=  6 ... t0=  48 t1=  64 word_start=true "▁to"
# t0/t1 are centiseconds from the start of the file.
TOKEN_RE = re.compile(r'^\s*\[\s*\d+\]\s.*?t0=\s*(\d+)\s+t1=\s*(\d+)\s+word_start=\w+\s+"(.*)"\s*$')
SENTENCE_END = (".", "?", "!")
MAX_SEGMENT_SECONDS = 30.0

# How often the main thread checks whether the job was cancelled, so stopping
# takes effect in about a second instead of at the end of the recording.
CANCEL_POLL_SECONDS = 1.0


def parse_tokens(stderr: str) -> List[Dict[str, Any]]:
    tokens = []
    for line in stderr.splitlines():
        m = TOKEN_RE.match(line)
        if m:
            tokens.append({"t0": int(m.group(1)) / 100, "t1": int(m.group(2)) / 100, "text": m.group(3)})
    return tokens


def tokens_to_segments(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group sub-word tokens into sentence-sized segments with start/end seconds."""
    segments, pieces, start = [], [], None

    def flush(end: float):
        text = "".join(pieces).replace("\u2581", " ").strip()
        if text:
            segments.append({"id": len(segments), "start": round(start, 2), "end": round(end, 2), "text": text})

    for i, tok in enumerate(tokens):
        if start is None:
            start = tok["t0"]
        pieces.append(tok["text"])
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        next_starts_word = nxt is None or nxt["text"].startswith("\u2581")
        sentence_done = tok["text"].endswith(SENTENCE_END) and next_starts_word
        too_long = tok["t1"] - start >= MAX_SEGMENT_SECONDS and next_starts_word
        if nxt is None or sentence_done or too_long:
            flush(tok["t1"])
            pieces, start = [], None
    return segments


def probe_duration(path: Path) -> Optional[float]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return round(float(res.stdout.strip()), 2)
    except ValueError:
        return None


def to_windows_path(path: Path) -> str:
    """parakeet-cli.exe is a Windows binary; it needs D:/... style paths."""
    res = subprocess.run(["wslpath", "-m", str(path)], capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"wslpath failed for {path}: {res.stderr.strip()}")
    return res.stdout.strip()


class ParakeetService:
    _inference_semaphore = threading.Semaphore(1)

    @staticmethod
    def supports(language: Optional[str]) -> bool:
        return language is None or language.lower() in PARAKEET_LANGUAGES

    @classmethod
    def transcribe(
        cls,
        audio_path: Path,
        language: Optional[str] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Transcribe one file with parakeet-cli.

        The CLI reports nothing until it exits - every token line of a 4-minute
        file arrives in a single burst at the end - so there is no progress to
        stream. stderr is drained on a thread anyway, because a 20-minute chunk
        produces far more than a pipe buffer holds and the child would block;
        that leaves the main thread free to poll for cancellation and terminate.
        """
        if not settings.parakeet_cli.exists():
            raise FileNotFoundError(f"parakeet-cli not found: {settings.parakeet_cli}")
        if not settings.parakeet_model.exists():
            raise FileNotFoundError(f"Parakeet model not found: {settings.parakeet_model}")

        with cls._inference_semaphore, tempfile.TemporaryDirectory(dir=settings.cache_dir) as tmp:
            source = Path(audio_path)
            if source.suffix.lower() not in NATIVE_FORMATS:
                wav = Path(tmp) / "audio.wav"
                conv = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                    capture_output=True, text=True,
                )
                if conv.returncode != 0:
                    raise RuntimeError(f"Audio conversion failed: {conv.stderr[-300:]}")
                source = wav

            # Probed up front: progress needs the total to divide by.
            duration = probe_duration(source)

            # stdout goes to a file rather than a pipe: a long transcript would
            # otherwise fill the pipe buffer and deadlock while we read stderr.
            stdout_file = Path(tmp) / "transcript.txt"
            stderr_lines: List[str] = []
            cmd = [
                str(settings.parakeet_cli),
                "-m", to_windows_path(settings.parakeet_model),
                "-f", to_windows_path(source),
                "-t", str(settings.parakeet_threads),
                "-np", "-ps",
            ]

            with open(stdout_file, "wb") as out:
                proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.PIPE)

                def _drain_stderr():
                    try:
                        for raw in iter(proc.stderr.readline, b""):
                            stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
                    except Exception:
                        pass

                drain = threading.Thread(target=_drain_stderr, name="parakeet-stderr", daemon=True)
                drain.start()

                cancelled = False
                while proc.poll() is None:
                    if should_cancel and should_cancel():
                        cancelled = True
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                    time.sleep(CANCEL_POLL_SECONDS)

                returncode = proc.wait()
                drain.join(timeout=5)
                if proc.stderr:
                    try:
                        proc.stderr.close()
                    except Exception:
                        pass

                if cancelled:
                    raise TranscriptionCancelled(f"Transcription of {audio_path.name} cancelled")

            stdout = stdout_file.read_text(encoding="utf-8", errors="replace").strip()
            stderr = "\n".join(stderr_lines)

        res_returncode = returncode
        # parakeet-cli exits 0 even when it cannot read the file, so an empty transcript is the failure signal.
        if res_returncode != 0 or not stdout:
            tail = " | ".join(line for line in stderr.splitlines() if not TOKEN_RE.match(line))[-400:]
            raise RuntimeError(f"Parakeet transcription failed (exit {res_returncode}): {tail}")

        tokens = parse_tokens(stderr)
        segments = tokens_to_segments(tokens)
        full_text = " ".join(line.strip() for line in stdout.splitlines() if line.strip())
        if duration is None and tokens:
            duration = round(tokens[-1]["t1"], 2)

        return {
            "engine": "parakeet",
            "model": settings.parakeet_model.stem.removeprefix("ggml-"),
            # Parakeet does not report the detected language.
            "language": language or "auto",
            "language_probability": None,
            "duration": duration or 0,
            "segments": segments,
            "full_text": full_text,
        }
