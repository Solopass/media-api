import os
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.config import settings

PRESET_INSTRUCTIONS = {
    "executive": (
        "You are the executive intelligence summarizer on the SOL workstation.\n"
        "Generate a crisp, structured Markdown summary of the following transcript.\n"
        "Include: (1) Core Executive Briefing, (2) Key Themes & Decisions, (3) Deliverables & Actionable Takeaways.\n"
        "Use bold keywords. Never invent facts, dates, or numbers not present in the transcript."
    ),
    "meeting": (
        "You are an expert executive scribe on the SOL workstation.\n"
        "Generate professional Meeting Minutes from the following transcript.\n"
        "Structure: (1) Participants/Speakers Mentioned, (2) Agenda Topics Discussed, (3) Key Decisions Reached, (4) Action Item Ownership Table (| Owner | Action Item | Target |).\n"
        "Never invent facts, dates, or numbers not present in the transcript."
    ),
    "timeline": (
        "You are a media archivist on the SOL workstation.\n"
        "Generate a Chronological Topic Breakdown & Timeline from the following transcript.\n"
        "Structure: A chronological table of contents tracking major topic shifts, key arguments, and quotes.\n"
        "Never invent facts, dates, or numbers not present in the transcript."
    ),
    "technical": (
        "You are a principal systems architect on the SOL workstation.\n"
        "Generate a Technical Deep Dive summary from the following transcript.\n"
        "Structure: (1) Architectural & System Overview, (2) APIs, Protocols & Schemas, (3) Technical Trade-offs & Constraints, (4) Implementation Notes.\n"
        "Never invent facts, dates, or numbers not present in the transcript."
    ),
    "bullets": (
        "You are a concise information extractor on the SOL workstation.\n"
        "Generate a dense, high-signal bulleted summary of the following transcript.\n"
        "Use bold lead-ins for each bullet. Omit conversational filler and pleasantries.\n"
        "Never invent facts, dates, or numbers not present in the transcript."
    )
}

PRESET_TITLES = {
    "executive": "Executive Briefing",
    "meeting": "Meeting Minutes & Decisions",
    "timeline": "Chronological Timeline & Topics",
    "technical": "Technical Architecture Deep Dive",
    "bullets": "Key Takeaways (Bullets)"
}

def summarize_with_ollama(
    title: str,
    text: str,
    model_name: str = "sol-fast",
    preset: str = "executive",
    focus: Optional[str] = None
) -> str:
    """Query local Ollama chat API instance for a structured briefing summary."""
    system_instruction = PRESET_INSTRUCTIONS.get(preset, PRESET_INSTRUCTIONS["executive"])
    focus_directive = f"\n\nSpecial User Focus Directive: Prioritize and thoroughly detail any discussion or facts regarding: '{focus.strip()}'." if focus and focus.strip() else ""

    # Smart prompt truncation: stay comfortably within the 8,192 context limit
    max_chars = settings.ollama_max_chars
    if len(text) > max_chars:
        head_len = int(max_chars * 0.65)
        tail_len = max_chars - head_len
        condensed_text = f"{text[:head_len]}\n\n[... middle section condensed for context limit ...]\n\n{text[-tail_len:]}"
    else:
        condensed_text = text

    user_prompt = f"Title: {title}\n\nTranscript:\n{condensed_text}{focus_directive}\n\nSummary:"
    req_data = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
        "options": {
            "num_ctx": 8192,
            "temperature": 0.3,
            "seed": 7
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        settings.ollama_api_url,
        data=req_data,
        headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=settings.ollama_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.time() - t0
            msg = data.get("message", {})
            content = msg.get("content") or data.get("response", "")
            
            # Strip internal <think> tags if present in raw content
            if content:
                import re
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

            prompt_eval_s = data.get("prompt_eval_duration", 0) / 1e9
            eval_s = data.get("eval_duration", 0) / 1e9
            eval_tokens = data.get("eval_count", 0)
            logger.info(
                f"[summarize] Summary generated via {model_name} in {elapsed:.1f}s "
                f"(prompt: {prompt_eval_s:.1f}s, gen: {eval_s:.1f}s, tokens: {eval_tokens})"
            )

            if content:
                return content
            return "*(Ollama returned empty summary)*"
    except Exception as e:
        elapsed = time.time() - t0
        logger.warning(f"[summarize] Ollama summary failed after {elapsed:.1f}s via {model_name}: {e}")
        return f"*(Automatic LLM summary unavailable: {e})*"

class ObsidianService:
    @staticmethod
    def create_transcript_note(
        media_info: Dict[str, Any],
        transcription: Dict[str, Any],
        summary: str,
        vault_subfolder: str = "Transcripts",
        custom_tags: Optional[List[str]] = None,
        summary_preset: str = "executive",
        summary_focus: Optional[str] = None
    ) -> Path:
        import re
        stt_label = transcription.get("model") or "unknown"
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        raw_title = media_info.get("title") or Path(media_info.get("file_path", "transcript")).stem
        # Strip illegal characters for filesystem & filenames
        safe_title = media_info.get("safe_title") or re.sub(r'[\\/*?:"<>|]', "", raw_title)
        safe_title = re.sub(r"\s+", "_", safe_title)[:80].strip("._") or "transcript"
        note_filename = f"{date_id}_{safe_title}.md"

        clean_yaml_title = raw_title.replace('"', '\\"').replace("\n", " ")
        clean_source_url = str(media_info.get("url", "")).replace('"', '\\"')

        # Sanitize and resolve target subfolder
        clean_sub = re.sub(r"\.\.+[/\\]?", "", vault_subfolder or "Transcripts").strip("/\\")
        if not clean_sub:
            clean_sub = "Transcripts"

        if clean_sub == "Transcripts" or clean_sub.startswith("Transcripts/"):
            sub_rel = clean_sub[len("Transcripts"):].strip("/\\")
            target_vault_dir = settings.vault_transcripts / sub_rel if sub_rel else settings.vault_transcripts
        else:
            target_vault_dir = settings.vault_transcripts.parent / clean_sub

        # Merge tags
        tags = ["media", "transcript", "ai-ingest", "obsidian-second-brain"]
        if summary_preset and summary_preset != "executive":
            tags.append(f"summary-{summary_preset}")
        if custom_tags:
            for t in custom_tags:
                ct = re.sub(r"[^\w\-]", "", str(t).strip().lower().lstrip("#"))
                if ct and ct not in tags:
                    tags.append(ct)
        tags_yaml = ", ".join(tags)

        # Preset label for callout
        callout_title = PRESET_TITLES.get(summary_preset, "Executive AI Summary")
        focus_notice = f"> *(User focus: {summary_focus.strip()})*\n> \n" if summary_focus and summary_focus.strip() else ""

        # Format markdown note
        note_lines = [
            "---",
            f"title: \"{clean_yaml_title}\"",
            f"date: {date_str}",
            f"tags: [{tags_yaml}]",
            f"source_url: \"{clean_source_url}\"",
            f"duration: \"{transcription.get('duration', 0)}s\"",
            f"stt_model: \"{stt_label}\"",
            f"language: \"{transcription.get('language', 'auto')}\"",
            "---",
            "",
            f"# 🎙️ {clean_yaml_title}",
            "",
            f"> [!abstract] **{callout_title}**",
            focus_notice + "> " + summary.replace("\n", "\n> "),
            "",
            "---",
            "",
            "## 📋 Metadata",
            f"- **Source Media**: `{media_info.get('file_path', '')}`",
            f"- **Uploader/Author**: {media_info.get('uploader', 'Unknown')}",
            f"- **Audio Duration**: {transcription.get('duration', 0)}s",
            f"- **Processed By**: Workstation Media API (`{stt_label}`)",
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
        vault_file = target_vault_dir / note_filename
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        with open(vault_file, "w", encoding="utf-8") as f:
            f.write(content)

        output_file = settings.output_transcripts / note_filename
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        # Update Transcripts_Dashboard.md
        ObsidianService.update_dashboard(media_info.get("title", safe_title), vault_file, subfolder=clean_sub)
        return vault_file

    @staticmethod
    def update_dashboard(title: str, note_path: Path, subfolder: str = "Transcripts"):
        dash = settings.vault_dashboard
        clean_sub = subfolder.strip("/\\") or "Transcripts"
        row = f"| {datetime.now().strftime('%Y-%m-%d')} | [[{clean_sub}/{note_path.stem}|{title[:40]}]] | Workstation Media API | ✅ Ingested |\n"
        try:
            if not dash.exists():
                dash.parent.mkdir(parents=True, exist_ok=True)
                with open(dash, "w", encoding="utf-8") as f:
                    f.write("# 🎙️ Transcripts & Media Dashboard\n\n| Date | Note | Engine | Status |\n| :--- | :--- | :--- | :--- |\n")
            with open(dash, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception:
            pass

    @staticmethod
    def create_error_report_note(
        media_info: Dict[str, Any],
        failed_stage: str,
        error_message: str,
        traceback_str: Optional[str] = None,
        partial_files: Optional[Dict[str, str]] = None,
        vault_subfolder: str = "Transcripts"
    ) -> Path:
        import re
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        raw_title = media_info.get("title") or Path(media_info.get("file_path", "unknown")).stem
        safe_title = media_info.get("safe_title") or re.sub(r'[\\/*?:"<>|]', "", raw_title)
        safe_title = re.sub(r"\s+", "_", safe_title)[:80].strip("._") or "media_job"
        note_filename = f"{date_id}_FAILED_{safe_title}.md"

        clean_yaml_title = raw_title.replace('"', '\\"').replace("\n", " ")
        clean_source_url = str(media_info.get("url", "")).replace('"', '\\"')

        clean_sub = re.sub(r"\.\.+[/\\]?", "", vault_subfolder or "Transcripts").strip("/\\")
        if not clean_sub:
            clean_sub = "Transcripts"

        if clean_sub == "Transcripts" or clean_sub.startswith("Transcripts/"):
            sub_rel = clean_sub[len("Transcripts"):].strip("/\\")
            target_vault_dir = settings.vault_transcripts / sub_rel if sub_rel else settings.vault_transcripts
        else:
            target_vault_dir = settings.vault_transcripts.parent / clean_sub

        note_lines = [
            "---",
            f"title: \"FAILED: {clean_yaml_title}\"",
            f"date: {date_str}",
            f"tags: [media, error-report, failed-ingest, obsidian-second-brain]",
            f"source_url: \"{clean_source_url}\"",
            f"failed_stage: \"{failed_stage}\"",
            "status: \"failed\"",
            "---",
            "",
            f"# ⚠️ Pipeline Error: {clean_yaml_title}",
            "",
            f"> [!danger] **Glitched Ingest - Terminated at Stage: {failed_stage}**",
            f"> **Error Message**: `{error_message}`",
            "",
            "---",
            "",
            "## 📋 Job Diagnostics",
            f"- **Source Media**: `{media_info.get('file_path') or media_info.get('url') or 'Unknown'}`",
            f"- **Failed Stage**: {failed_stage}",
            f"- **Timestamp**: {date_str}",
            f"- **Processed By**: Workstation Media API (Diagnostic Auto-Capture)",
            ""
        ]

        if partial_files:
            note_lines.extend([
                "### 💾 Partial Files Preserved",
                ""
            ])
            for k, v in partial_files.items():
                note_lines.append(f"- **{k.capitalize()}**: `{v}`")
            note_lines.append("")

        if traceback_str:
            note_lines.extend([
                "---",
                "",
                "## 🔍 Stack Trace Details",
                "",
                "```text",
                traceback_str.strip(),
                "```",
                ""
            ])

        content = "\n".join(note_lines)

        vault_file = target_vault_dir / note_filename
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        with open(vault_file, "w", encoding="utf-8") as f:
            f.write(content)

        output_file = settings.output_transcripts / note_filename
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        # Record error in dashboard
        dash = settings.vault_dashboard
        if dash.exists():
            row = f"| {datetime.now().strftime('%Y-%m-%d')} | [[{clean_sub}/{vault_file.stem}|FAILED: {raw_title[:32]}]] | Workstation Media API | ❌ Failed ({failed_stage}) |\n"
            try:
                with open(dash, "a", encoding="utf-8") as f:
                    f.write(row)
            except Exception:
                pass

        return vault_file

    @staticmethod
    def update_note_summary(
        note_path: Path,
        new_summary: str,
        summary_preset: str = "executive",
        summary_focus: Optional[str] = None
    ) -> bool:
        """Update or replace the abstract callout in an existing transcript note."""
        import re
        if not note_path.exists():
            return False

        content = note_path.read_text(encoding="utf-8")
        callout_title = PRESET_TITLES.get(summary_preset, "Executive AI Summary")
        focus_notice = f"> *(User focus: {summary_focus.strip()})*\n> \n" if summary_focus and summary_focus.strip() else ""
        new_block = f"> [!abstract] **{callout_title}**\n{focus_notice}> " + new_summary.replace("\n", "\n> ")

        # Match from > [!abstract] until the next markdown header or divider
        pattern = r"> \[!abstract\].*?(?=\n\n---|\n\n##|\Z)"
        if re.search(pattern, content, flags=re.DOTALL):
            updated_content = re.sub(pattern, new_block, content, count=1, flags=re.DOTALL)
        else:
            # If no callout found, insert below the title header
            title_pat = r"(# 🎙️ .*\n)"
            if re.search(title_pat, content):
                updated_content = re.sub(title_pat, r"\1\n" + new_block + "\n", content, count=1)
            else:
                updated_content = new_block + "\n\n" + content

        note_path.write_text(updated_content, encoding="utf-8")

        # Also update mirror file in settings.output_transcripts if present
        mirror_file = settings.output_transcripts / note_path.name
        if mirror_file.exists():
            mirror_file.write_text(updated_content, encoding="utf-8")

        return True

