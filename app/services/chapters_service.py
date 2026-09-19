"""
YouTube-style chapter generation from a timestamped transcript.

Two things make this work rather than hallucinate:

1. The model is shown *timestamps*. The summary path sends plain text, so it has
   nothing to anchor a chapter to. Here the transcript is downsampled evenly
   across its whole duration into `[MM:SS] text` lines, which also avoids the
   summary path's head/tail truncation dropping the middle of a long video.
2. Nothing the model returns is trusted. Every timestamp is parsed, bounded by
   the real duration, forced into increasing order and deduplicated; if too few
   survive, the result is no chapters rather than wrong ones.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.obsidian_service import OllamaError, _ollama_chat

logger = logging.getLogger("media_api.chapters")

MIN_CHAPTERS = 2
MAX_CHAPTERS = 20

# Chapters closer together than this are redundant for navigation.
MIN_GAP_SECONDS = 20.0

# Digest line sizing: enough text per anchor to be meaningful, plus the
# "[MM:SS] " prefix and newline.
MIN_LINE_CHARS = 40
TIMESTAMP_OVERHEAD = 12

CHAPTER_INSTRUCTION = (
    "You are a video chapter editor on the SOL workstation.\n"
    "You are given a transcript sampled at timestamps across a recording.\n"
    "Produce YouTube-style chapter markers covering the whole recording.\n"
    "\n"
    "Rules:\n"
    "- Output ONLY lines of the form 'MM:SS Title' (or 'HH:MM:SS Title' past an hour).\n"
    "- The first line MUST start at 00:00.\n"
    "- Between 3 and 12 chapters, in increasing time order.\n"
    "- Use only timestamps that appear in the transcript sample.\n"
    "- Titles are 2 to 6 words, describing what is actually discussed there.\n"
    "- No commentary, numbering, bullets or markdown. Nothing but the chapter lines."
)

# '03:45 Title', '[03:45] - Title', '1:02:03 — Title'
CHAPTER_LINE = re.compile(
    r"^\s*[-*•]?\s*\[?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]?\s*[-–—:.)]?\s+(.+?)\s*$"
)


def timestamp_to_seconds(stamp: str) -> Optional[float]:
    """'03:45' or '1:02:03' -> seconds. None when it is not a timestamp."""
    parts = stamp.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def seconds_to_timestamp(seconds: float) -> str:
    """Seconds -> 'MM:SS', or 'H:MM:SS' past an hour (YouTube's own format)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_timestamped_digest(
    segments: List[Dict[str, Any]],
    max_chars: Optional[int] = None,
    target_lines: int = 60,
) -> str:
    """
    Downsample segments into `[MM:SS] text` lines spread across the full duration.

    Coverage is uniform by construction: the transcript is split into
    `target_lines` even buckets and one line is emitted per non-empty bucket, so
    a three-hour recording still offers the model anchors in its middle hour.
    """
    usable = [
        s for s in segments
        if isinstance(s, dict) and s.get("text") and s.get("start") is not None
    ]
    if not usable:
        return ""

    budget = max_chars or settings.ollama_max_chars
    duration = max(float(s.get("end") or s.get("start") or 0.0) for s in usable)
    if duration <= 0:
        duration = float(usable[-1].get("start") or 0.0) or 1.0

    # Bucket count must fit the char budget, or the tail gets truncated away and
    # coverage silently stops partway through the recording.
    min_line_chars = MIN_LINE_CHARS + TIMESTAMP_OVERHEAD
    max_lines_in_budget = max(1, budget // min_line_chars)
    bucket_count = max(1, min(target_lines, len(usable), max_lines_in_budget))
    bucket_span = duration / bucket_count
    per_line_budget = max(MIN_LINE_CHARS, int(budget / bucket_count) - TIMESTAMP_OVERHEAD)

    lines: List[str] = []
    current_bucket = -1
    for segment in usable:
        start = float(segment.get("start") or 0.0)
        bucket = int(start / bucket_span) if bucket_span > 0 else 0
        if bucket == current_bucket:
            continue
        current_bucket = bucket
        text = " ".join(str(segment["text"]).split())[:per_line_budget]
        if text:
            lines.append(f"[{seconds_to_timestamp(start)}] {text}")

    digest = "\n".join(lines)
    if len(digest) > budget:
        digest = digest[:budget].rsplit("\n", 1)[0]
    return digest


def parse_chapters(raw: str, duration: float) -> List[Dict[str, Any]]:
    """
    Turn a model response into chapters that can be trusted.

    Drops timestamps past the real duration, enforces increasing order with a
    minimum gap, dedupes titles, forces the first chapter to 00:00, and caps the
    count. Returns [] when fewer than MIN_CHAPTERS survive, so a bad generation
    degrades to no chapters instead of wrong ones.
    """
    if not raw:
        return []

    limit = float(duration) if duration and duration > 0 else None
    parsed: List[Dict[str, Any]] = []
    seen_titles = set()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        match = CHAPTER_LINE.match(line)
        if not match:
            continue
        seconds = timestamp_to_seconds(match.group(1))
        if seconds is None:
            continue
        title = re.sub(r"[*_`#]", "", match.group(2)).strip(" -–—:")
        if not title:
            continue
        if limit is not None and seconds > limit:
            logger.info(f"[chapters] Dropping '{title}' at {seconds:.0f}s: past duration {limit:.0f}s")
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        parsed.append({"start": float(seconds), "title": title[:80]})

    if not parsed:
        return []

    parsed.sort(key=lambda c: c["start"])

    ordered: List[Dict[str, Any]] = []
    for chapter in parsed:
        if ordered and chapter["start"] - ordered[-1]["start"] < MIN_GAP_SECONDS:
            continue
        ordered.append(chapter)

    if len(ordered) < MIN_CHAPTERS:
        logger.warning(f"[chapters] Only {len(ordered)} valid chapter(s) parsed; discarding")
        return []

    ordered = ordered[:MAX_CHAPTERS]
    # A chapter list that does not start at the beginning is not usable on
    # YouTube and looks broken in the note.
    ordered[0]["start"] = 0.0

    for chapter in ordered:
        chapter["timestamp"] = seconds_to_timestamp(chapter["start"])

    return ordered


def generate_chapters(
    title: str,
    segments: List[Dict[str, Any]],
    duration: float,
    model_name: str = "sol-fast",
) -> List[Dict[str, Any]]:
    """Digest -> local LLM -> validated chapters. Never raises; [] means none."""
    digest = build_timestamped_digest(segments)
    if not digest:
        logger.info("[chapters] No timestamped segments available; skipping")
        return []

    user_prompt = (
        f"Title: {title}\n"
        f"Total duration: {seconds_to_timestamp(duration)}\n\n"
        f"Transcript sample:\n{digest}\n\n"
        "Chapters:"
    )
    try:
        raw = _ollama_chat(
            CHAPTER_INSTRUCTION, user_prompt, model_name, label="chapters", temperature=0.2
        )
    except OllamaError as e:
        logger.warning(f"[chapters] Generation unavailable: {e}")
        return []

    chapters = parse_chapters(raw, duration)
    logger.info(f"[chapters] Accepted {len(chapters)} chapter(s) from {model_name}")
    return chapters


def to_youtube_block(chapters: List[Dict[str, Any]]) -> str:
    """The plain 'MM:SS Title' block that pastes into a YouTube description."""
    return "\n".join(f"{c['timestamp']} {c['title']}" for c in chapters)
