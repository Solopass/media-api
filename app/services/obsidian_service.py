import os
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from app.config import settings

def summarize_with_ollama(title: str, text: str, model_name: str = "qwen2.5-coder:7b") -> str:
    """Query local Ollama instance for an executive briefing summary."""
    prompt = (
        f"You are the executive intelligence summarizer on the GAMINGPC workstation.\n"
        f"Generate a crisp, structured Markdown summary of the following transcript.\n"
        f"Use bullet points, bold keywords, and an 'Actionable Takeaways' section.\n\n"
        f"Title: {title}\n\n"
        f"Transcript:\n{text[:12000]}\n\n"
        f"Summary:"
    )
    req_data = json.dumps({
        "model": model_name,
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")

    req = urllib.request.Request(
        settings.ollama_api_url,
        data=req_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
    except Exception as e:
        return f"*(Automatic LLM summary unavailable: {e})*"

class ObsidianService:
    @staticmethod
    def create_transcript_note(
        media_info: Dict[str, Any],
        transcription: Dict[str, Any],
        summary: str,
        model_size: str = "tiny"
    ) -> Path:
        import re
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        raw_title = media_info.get("title") or Path(media_info.get("file_path", "transcript")).stem
        # Strip illegal characters for filesystem & filenames
        safe_title = media_info.get("safe_title") or re.sub(r'[\\/*?:"<>|]', "", raw_title)
        safe_title = re.sub(r"\s+", "_", safe_title)[:80].strip("._") or "transcript"
        note_filename = f"{date_id}_{safe_title}.md"

        clean_yaml_title = raw_title.replace('"', '\\"').replace("\n", " ")
        clean_source_url = str(media_info.get("url", "")).replace('"', '\\"')

        # Format markdown note
        note_lines = [
            "---",
            f"title: \"{clean_yaml_title}\"",
            f"date: {date_str}",
            "tags: [media, transcript, ai-ingest, obsidian-second-brain]",
            f"source_url: \"{clean_source_url}\"",
            f"duration: \"{transcription.get('duration', 0)}s\"",
            f"whisper_model: \"{model_size}\"",
            f"language: \"{transcription.get('language', 'auto')}\"",
            "---",
            "",
            f"# 🎙️ {clean_yaml_title}",
            "",
            "> [!abstract] **Executive AI Summary**",
            "> " + summary.replace("\n", "\n> "),
            "",
            "---",
            "",
            "## 📋 Metadata",
            f"- **Source Media**: `{media_info.get('file_path', '')}`",
            f"- **Uploader/Author**: {media_info.get('uploader', 'Unknown')}",
            f"- **Audio Duration**: {transcription.get('duration', 0)}s",
            f"- **Processed By**: Workstation Media API (`faster-whisper` {model_size})",
            "",
            "---",
            "",
            "## 📝 Full Timestamped Transcript",
            "",
            "| Timestamp | Speaker / Audio Content |",
            "| :--- | :--- |"
        ]

        for s in transcription.get("segments", []):
            start_m = int(s["start"] // 60)
            start_s = int(s["start"] % 60)
            time_tag = f"`{start_m:02d}:{start_s:02d}`"
            note_lines.append(f"| {time_tag} | {s['text']} |")

        content = "\n".join(note_lines)

        # Write to Vault and Output directories
        vault_file = settings.vault_transcripts / note_filename
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        with open(vault_file, "w", encoding="utf-8") as f:
            f.write(content)

        output_file = settings.output_transcripts / note_filename
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        # Update Transcripts_Dashboard.md
        ObsidianService.update_dashboard(media_info.get("title", safe_title), vault_file)
        return vault_file

    @staticmethod
    def update_dashboard(title: str, note_path: Path):
        dash = settings.vault_dashboard
        if not dash.exists():
            return
        row = f"| {datetime.now().strftime('%Y-%m-%d')} | [[Transcripts/{note_path.stem}|{title[:40]}]] | Workstation Media API | ✅ Ingested |\n"
        try:
            with open(dash, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception:
            pass
