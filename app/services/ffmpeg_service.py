import re
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from app.config import settings

def parse_timecode(tc: str) -> float:
    """Parse HH:MM:SS or MM:SS or raw seconds into float seconds."""
    tc = tc.strip()
    if re.match(r"^\d+(\.\d+)?$", tc):
        return float(tc)
    parts = tc.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid timestamp format: {tc}")

def format_timecode(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

class FFmpegService:
    @staticmethod
    def clip_media(
        source_path: Path,
        start_time: str,
        end_time: str,
        target_format: str = "mp4",
        re_encode: bool = False,
        out_dir: Optional[Path] = None
    ) -> Path:
        start_sec = parse_timecode(start_time)
        end_sec = parse_timecode(end_time)
        if end_sec <= start_sec:
            raise ValueError("end_time must be greater than start_time")

        duration = end_sec - start_sec
        dest_dir = out_dir or settings.output_videos
        dest_dir.mkdir(parents=True, exist_ok=True)

        stem = source_path.stem
        tag = f"clip_{int(start_sec)}s_{int(end_sec)}s"
        out_file = dest_dir / f"{stem}_{tag}.{target_format}"

        # Fast keyframe copy vs precision re-encode
        if re_encode:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", str(source_path),
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                str(out_file)
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", str(source_path),
                "-t", str(duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(out_file)
            ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            # Fallback to re-encoding if stream copy fails
            cmd_fallback = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", str(source_path),
                "-t", str(duration),
                "-c:v", "libx264", "-c:a", "aac",
                str(out_file)
            ]
            res2 = subprocess.run(cmd_fallback, capture_output=True, text=True)
            if res2.returncode != 0:
                raise RuntimeError(f"FFmpeg error: {res2.stderr[:400]}")

        return out_file

    @staticmethod
    def extract_audio(source_path: Path, target_format: str = "mp3") -> Path:
        dest_dir = settings.output_audio
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_file = dest_dir / f"{source_path.stem}.{target_format}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_path),
            "-vn",
            "-acodec", "libmp3lame" if target_format == "mp3" else "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            str(out_file)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Audio extraction failed: {res.stderr[:300]}")
        return out_file
