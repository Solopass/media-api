"""
Splitting long audio into transcribable chunks.

parakeet-cli cannot decode a long recording: a 9h05m book exits 5 during
`read_audio_data` at both 44.1 kHz stereo (499 MB) and 16 kHz mono (93.6 MB),
while 30 min, 1h, 2h and 4h all decode fine. 9.08h x 16 kHz x 4 bytes is 1.95 GB,
right at a 2 GB float32 ceiling, so the limit is duration rather than format.

Chunking removes that ceiling, and also buys real per-chunk progress, prompt
cancellation, and a failure that costs one chunk instead of the whole book.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("media_api.chunks")

# 4h is proven to decode, so this is deep headroom. The size is chosen for
# progress granularity and cancellation latency, not for the ceiling.
CHUNK_TARGET_SECONDS = 1200.0

# A cut is moved to the nearest silence within this window so a sentence is not
# sliced mid-word.
SILENCE_SNAP_WINDOW = 20.0
SILENCE_NOISE_FLOOR = "-30dB"
SILENCE_MIN_DURATION = 0.4

# Never emit a sliver: merge anything shorter than this into the previous chunk.
MIN_CHUNK_SECONDS = 60.0

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


@dataclass
class AudioChunk:
    index: int
    start: float
    duration: float
    path: Path

    @property
    def end(self) -> float:
        return self.start + self.duration


def needs_chunking(duration_seconds: float, target: float = CHUNK_TARGET_SECONDS) -> bool:
    """Short recordings keep the simpler single-shot path."""
    return duration_seconds > target


def detect_silences(audio_path: Path, timeout: float = 900.0) -> List[float]:
    """
    Candidate cut points: the midpoint of each detected silent stretch.

    One decode pass with ffmpeg's silencedetect. Failure is not fatal - without
    candidates the cuts simply land on their targets.
    """
    cmd = [
        "ffmpeg", "-v", "info", "-i", str(audio_path),
        "-af", f"silencedetect=noise={SILENCE_NOISE_FLOOR}:d={SILENCE_MIN_DURATION}",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        logger.warning(f"[chunks] silencedetect unavailable ({e}); cutting on targets")
        return []

    midpoints: List[float] = []
    pending_start: Optional[float] = None
    for line in res.stderr.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            pending_start = float(start_match.group(1))
            continue
        end_match = SILENCE_END_RE.search(line)
        if end_match and pending_start is not None:
            end = float(end_match.group(1))
            midpoints.append((pending_start + end) / 2)
            pending_start = None

    logger.info(f"[chunks] Found {len(midpoints)} silence candidates in {audio_path.name}")
    return midpoints


def plan_cuts(
    duration: float,
    silences: List[float],
    target: float = CHUNK_TARGET_SECONDS,
    window: float = SILENCE_SNAP_WINDOW,
) -> List[float]:
    """
    Boundaries for the chunks, starting at 0.0 and ending at duration.

    Each interior boundary starts at a multiple of the target and moves to the
    nearest silence within the window, when there is one.
    """
    if duration <= target:
        return [0.0, duration]

    boundaries = [0.0]
    position = target
    while position < duration - MIN_CHUNK_SECONDS:
        candidates = [s for s in silences if abs(s - position) <= window and s > boundaries[-1]]
        cut = min(candidates, key=lambda s: abs(s - position)) if candidates else position
        if cut - boundaries[-1] >= MIN_CHUNK_SECONDS:
            boundaries.append(cut)
        position += target
    boundaries.append(duration)
    return boundaries


def write_chunks(audio_path: Path, boundaries: List[float], dest_dir: Path) -> List[AudioChunk]:
    """
    Cut the audio into 16 kHz mono wav pieces, the format the engine wants.

    wav avoids a second decode step, and a 20-minute piece is only ~38 MB.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    chunks: List[AudioChunk] = []

    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        duration = boundaries[index + 1] - start
        out_path = dest_dir / f"chunk_{index:03d}.wav"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start:.3f}",
            "-i", str(audio_path),
            "-t", f"{duration:.3f}",
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"Could not cut chunk {index}: {res.stderr[-300:]}")
        chunks.append(AudioChunk(index=index, start=start, duration=duration, path=out_path))

    return chunks


def split(audio_path: Path, dest_dir: Path, duration: float) -> List[AudioChunk]:
    """Detect, plan and cut in one call."""
    silences = detect_silences(audio_path)
    boundaries = plan_cuts(duration, silences)
    chunks = write_chunks(audio_path, boundaries, dest_dir)
    logger.info(
        f"[chunks] Split {audio_path.name} ({duration / 60:.0f} min) into {len(chunks)} chunks"
    )
    return chunks
