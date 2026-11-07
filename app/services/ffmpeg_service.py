import re
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Same rationale as the STT progress interval: report often enough to look
# alive, rarely enough to cost nothing.
PROGRESS_INTERVAL_SECONDS = 2.0

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
        normalize_audio: bool = False,
        speech_optimized: bool = False,
        out_dir: Optional[Path] = None
    ) -> Path:
        start_sec = parse_timecode(start_time)
        end_sec = parse_timecode(end_time)
        if end_sec <= start_sec:
            raise ValueError("end_time must be greater than start_time")

        duration = end_sec - start_sec
        effective_format = "wav" if speech_optimized else target_format
        is_audio = effective_format in ["mp3", "wav", "m4a", "ogg", "flac"]
        dest_dir = out_dir or (settings.output_audio if is_audio else settings.output_videos)
        dest_dir.mkdir(parents=True, exist_ok=True)

        stem = source_path.stem
        tag_parts = [f"clip_{int(start_sec)}s_{int(end_sec)}s"]
        if normalize_audio:
            tag_parts.append("norm")
        if speech_optimized:
            tag_parts.append("16k")
        tag = "_".join(tag_parts)
        out_file = dest_dir / f"{stem}_{tag}.{effective_format}"

        # Speech-optimized 16kHz mono WAV for fast STT inference
        if speech_optimized:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", str(source_path),
                "-t", str(duration),
                "-vn",
                "-c:a", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1"
            ]
            if normalize_audio:
                cmd.extend(["-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11"])
            cmd.append(str(out_file))
        elif normalize_audio:
            # Re-encoding audio with EBU R128 loudness filter
            if is_audio:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start_sec),
                    "-i", str(source_path),
                    "-t", str(duration),
                    "-vn",
                    "-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:a", "libmp3lame" if effective_format == "mp3" else "pcm_s16le",
                    str(out_file)
                ]
            else:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start_sec),
                    "-i", str(source_path),
                    "-t", str(duration),
                    "-c:v", "libx264" if re_encode else "copy",
                    "-filter:a", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:a", "aac", "-b:a", "192k",
                    str(out_file)
                ]
        elif re_encode:
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
            # Fallback to general re-encoding if stream copy fails
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
    def extract_audio(
        source_path: Path,
        target_format: str = "mp3",
        progress_callback: Optional[Callable[[float, float], None]] = None,
    ) -> Path:
        """
        Extract an audio track for STT.

        progress_callback(fraction_done, seconds_done) is driven by ffmpeg's own
        -progress stream, so a multi-hour file reports continuously instead of
        looking frozen for several minutes.
        """
        dest_dir = settings.output_audio
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_file = dest_dir / f"{source_path.stem}.{target_format}"

        total_duration = 0.0
        if progress_callback:
            try:
                total_duration = float(FFmpegService.probe_local_media(source_path).get("duration") or 0.0)
            except Exception:
                total_duration = 0.0

        cmd = [
            "ffmpeg", "-y",
            "-i", str(source_path),
            "-vn",
            "-acodec", "libmp3lame" if target_format == "mp3" else "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
        ]
        if progress_callback and total_duration > 0:
            cmd += ["-progress", "pipe:1", "-nostats"]
        cmd.append(str(out_file))

        if not (progress_callback and total_duration > 0):
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Audio extraction failed: {res.stderr[:300]}")
            return out_file

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        last_emit = 0.0
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("out_time_ms="):
                    continue
                now = time.time()
                if now - last_emit < PROGRESS_INTERVAL_SECONDS:
                    continue
                last_emit = now
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                    progress_callback(min(1.0, done / total_duration), done, False)
                except Exception:
                    pass
        finally:
            if proc.stdout:
                proc.stdout.close()
            stderr_output = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            if proc.stderr:
                proc.stderr.close()
            returncode = proc.wait()

        if returncode != 0:
            raise RuntimeError(f"Audio extraction failed: {stderr_output[:300]}")
        return out_file

    @staticmethod
    def has_audio_stream(source_path: Path) -> bool:
        """
        True when the file carries at least one audio stream.

        X serves looping "GIFs" as MP4 with video only. Extracting audio from
        one fails, so the caller checks first and skips transcription instead.
        On any ffprobe trouble this answers True, so a probe failure cannot
        silently turn a normal video into a no-audio one.
        """
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(source_path)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
        except Exception:
            return True
        if res.returncode != 0:
            return True
        return bool(res.stdout.strip())

    @staticmethod
    def probe_local_media(source_path: Path) -> Dict[str, Any]:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        duration = 0.0
        if res.returncode == 0 and res.stdout.strip():
            try:
                duration = float(res.stdout.strip().split()[0])
            except Exception:
                duration = 0.0
        mins = int(duration // 60)
        secs = int(duration % 60)
        return {
            "title": source_path.stem,
            "uploader": "Local Media",
            "duration": round(duration, 2),
            "duration_formatted": f"{mins:02d}:{secs:02d}",
            "thumbnail": None,
            "description": f"Local media file: {source_path.name}"
        }
