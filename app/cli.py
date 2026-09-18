import sys
import os
import time
import json
import argparse
import urllib.request
import urllib.error
import subprocess

API_URL = "http://localhost:8080/api/v1"

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

def ensure_server():
    """Ensure media-api is reachable; socket activation will start it on first connect."""
    try:
        req = urllib.request.Request(f"{API_URL}/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        subprocess.run(["systemctl", "start", "media-api.socket"], capture_output=True)
        time.sleep(0.5)
        try:
            req = urllib.request.Request(f"{API_URL}/health")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"{RED}[!] Could not connect to Media API: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

def post_json(endpoint: str, data: dict) -> dict:
    ensure_server()
    url = f"{API_URL}{endpoint}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"{RED}[!] API Error ({e.code}): {err}{RESET}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"{RED}[!] Network Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

def get_json(endpoint: str) -> dict:
    ensure_server()
    url = f"{API_URL}{endpoint}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"{RED}[!] API Error ({e.code}): {err}{RESET}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"{RED}[!] Network Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

def wait_for_job(job_id: str, auto_close: bool = False):
    print(f"{CYAN}[*] Job queued (ID: {job_id}). Waiting for completion...{RESET}")
    last_msg = ""
    while True:
        job = get_json(f"/status/{job_id}")
        status = job.get("status")
        pct = job.get("progress_percent", 0.0)
        speed = job.get("speed") or ""
        eta = job.get("eta") or ""
        
        info = f"{status.upper()} | {pct:.1f}%"
        if speed: info += f" | {speed}"
        if eta: info += f" | ETA: {eta}"
        
        if info != last_msg:
            print(f"  -> {info}")
            last_msg = info
            
        if status == "completed":
            print(f"\n{GREEN}{BOLD}[OK] Job Completed Successfully!{RESET}")
            outputs = job.get("output_files", {})
            for k, v in outputs.items():
                print(f"  - {BOLD}{k}{RESET}: {v}")
            if job.get("obsidian_link"):
                print(f"  - {BOLD}vault_link{RESET}: {job.get('obsidian_link')}")
            if auto_close:
                print(f"{CYAN}[*] Releasing resources (--close)...{RESET}")
                post_json("/shutdown", {"force": False})
            break
        elif status in ("failed", "cancelled"):
            print(f"\n{RED}{BOLD}[FAIL] Job {status}: {job.get('error', 'Unknown error')}{RESET}")
            if auto_close:
                post_json("/shutdown", {"force": False})
            sys.exit(1)
            
        time.sleep(1)

def cmd_download(args):
    data = {
        "url": args.url,
        "format": args.format,
        "audio_only": args.audio_only,
        "priority": "normal"
    }
    res = post_json("/download", data)
    job_id = res["job_id"]
    if args.no_wait:
        print(f"{GREEN}[OK] Download queued: {job_id}{RESET}")
        print(f"Check status: media-status {job_id}")
    else:
        wait_for_job(job_id, auto_close=args.close)

def cmd_clip(args):
    data = {
        "source": args.source,
        "start_time": args.start,
        "end_time": args.end,
        "format": args.format,
        "re_encode": args.re_encode
    }
    res = post_json("/clip", data)
    job_id = res["job_id"]
    if args.no_wait:
        print(f"{GREEN}[OK] Clip queued: {job_id}{RESET}")
        print(f"Check status: media-status {job_id}")
    else:
        wait_for_job(job_id, auto_close=args.close)

def cmd_transcribe(args):
    data = {
        "source": args.source,
        "engine": args.engine,
        "model_size": args.model,
        "language": args.language
    }
    res = post_json("/transcribe", data)
    job_id = res["job_id"]
    if args.no_wait:
        print(f"{GREEN}[OK] Transcription queued: {job_id}{RESET}")
        print(f"Check status: media-status {job_id}")
    else:
        wait_for_job(job_id, auto_close=args.close)

def cmd_ingest(args):
    data = {
        "source": args.source,
        "clip_start": args.clip_start,
        "clip_end": args.clip_end,
        "engine": args.engine,
        "model_size": args.model,
        "summarize": not args.no_summary,
        "llm_model": args.llm_model
    }
    res = post_json("/ingest", data)
    job_id = res["job_id"]
    if args.no_wait:
        print(f"{GREEN}[OK] Full ingestion pipeline queued: {job_id}{RESET}")
        print(f"Check status: media-status {job_id}")
    else:
        wait_for_job(job_id, auto_close=args.close)

def cmd_status(args):
    job = get_json(f"/status/{args.job_id}")
    print(json.dumps(job, indent=2))

def cmd_shutdown(args):
    res = post_json("/shutdown", {"force": args.force})
    print(f"{GREEN}[OK] {res.get('message')}{RESET}")

def cmd_api(args):
    action = args.action
    if action == "status":
        subprocess.run(["systemctl", "status", "media-api.socket", "media-api.service", "--no-pager"])
    elif action == "start":
        subprocess.run(["systemctl", "start", "media-api.service"])
        print(f"{GREEN}[OK] media-api.service started.{RESET}")
    elif action == "stop":
        subprocess.run(["systemctl", "stop", "media-api.service"])
        print(f"{YELLOW}[OK] media-api.service stopped (returned to socket standby).{RESET}")
    elif action == "restart":
        subprocess.run(["systemctl", "restart", "media-api.service"])
        print(f"{GREEN}[OK] media-api.service restarted.{RESET}")
    elif action == "logs":
        subprocess.run(["journalctl", "-u", "media-api.service", "-f", "-n", "50"])
    elif action == "standby":
        subprocess.run(["systemctl", "stop", "media-api.service"])
        subprocess.run(["systemctl", "restart", "media-api.socket"])
        print(f"{GREEN}[OK] media-api is in 0-RAM socket standby mode.{RESET}")
    elif action == "update":
        print(f"{CYAN}[*] Triggering yt-dlp update check...{RESET}")
        res = post_json("/update-ytdlp", {})
        if res.get("status") == "success":
            if res.get("updated"):
                print(f"{GREEN}[✓] yt-dlp updated: {res.get('previous_version')} -> {res.get('version')}{RESET}")
            else:
                print(f"{GREEN}[✓] yt-dlp is already up to date ({res.get('version')}){RESET}")
        elif res.get("status") == "skipped":
            print(f"{YELLOW}[i] {res.get('message')} (version {res.get('version')}){RESET}")
        else:
            print(f"{RED}[!] Update error: {res.get('error')}{RESET}")

def cmd_update(args):
    print(f"{CYAN}[*] Triggering yt-dlp update check...{RESET}")
    res = post_json("/update-ytdlp", {})
    if res.get("status") == "success":
        if res.get("updated"):
            print(f"{GREEN}[✓] yt-dlp updated: {res.get('previous_version')} -> {res.get('version')}{RESET}")
        else:
            print(f"{GREEN}[✓] yt-dlp is already up to date ({res.get('version')}){RESET}")
    elif res.get("status") == "skipped":
        print(f"{YELLOW}[i] {res.get('message')} (version {res.get('version')}){RESET}")
    else:
        print(f"{RED}[!] Update error: {res.get('error')}{RESET}")

def main():
    if len(sys.argv) > 1 and sys.argv[1] in [
        "media-download", "media-clip", "media-transcribe",
        "media-ingest", "media-status", "media-shutdown", "media-api", "media-update",
        "download", "clip", "transcribe", "ingest", "status", "shutdown", "service", "update"
    ]:
        prog = sys.argv[1]
        args_to_parse = sys.argv[2:]
    else:
        prog = os.path.basename(sys.argv[0])
        args_to_parse = sys.argv[1:]
    
    if prog in ("media-download", "download"):
        parser = argparse.ArgumentParser(description="Download media via yt-dlp on E-cores")
        parser.add_argument("url", help="Target media URL")
        parser.add_argument("--format", default="mp4", choices=["mp4", "webm", "mp3", "wav", "m4a"], help="Output format")
        parser.add_argument("--audio-only", action="store_true", help="Download audio only")
        parser.add_argument("--no-wait", action="store_true", help="Do not wait for job completion")
        parser.add_argument("--close", action="store_true", help="Immediately shut down API after completion")
        cmd_download(parser.parse_args(args_to_parse))
        return

    if prog in ("media-clip", "clip"):
        parser = argparse.ArgumentParser(description="Precision slice/clip media via FFmpeg on E-cores")
        parser.add_argument("source", help="Media URL or local file path")
        parser.add_argument("start", help="Start timestamp (HH:MM:SS or seconds)")
        parser.add_argument("end", help="End timestamp (HH:MM:SS or seconds)")
        parser.add_argument("--format", default="mp4", help="Output format")
        parser.add_argument("--re-encode", action="store_true", help="Force re-encode vs stream copy")
        parser.add_argument("--no-wait", action="store_true", help="Do not wait for job completion")
        parser.add_argument("--close", action="store_true", help="Immediately shut down API after completion")
        cmd_clip(parser.parse_args(args_to_parse))
        return

    if prog in ("media-transcribe", "transcribe"):
        parser = argparse.ArgumentParser(description="Transcribe media via faster-whisper on AVX-512")
        parser.add_argument("source", help="Media URL or local file path")
        parser.add_argument("--engine", default=None, choices=["parakeet", "whisper"], help="STT engine (default: parakeet)")
        parser.add_argument("--model", default="tiny", choices=["tiny", "base", "small", "medium"], help="Whisper model size (whisper engine only)")
        parser.add_argument("--language", default=None, help="Spoken language code (e.g. en, es)")
        parser.add_argument("--no-wait", action="store_true", help="Do not wait for job completion")
        parser.add_argument("--close", action="store_true", help="Immediately shut down API after completion")
        cmd_transcribe(parser.parse_args(args_to_parse))
        return

    if prog in ("media-ingest", "ingest"):
        parser = argparse.ArgumentParser(description="Full ingestion: Download -> Whisper -> Ollama AI Summary -> Obsidian Vault")
        parser.add_argument("source", help="Media URL or local file path")
        parser.add_argument("--clip-start", default=None, help="Optional clip start timecode")
        parser.add_argument("--clip-end", default=None, help="Optional clip end timecode")
        parser.add_argument("--engine", default=None, choices=["parakeet", "whisper"], help="STT engine (default: parakeet)")
        parser.add_argument("--model", default="tiny", choices=["tiny", "base", "small", "medium"], help="Whisper model size (whisper engine only)")
        parser.add_argument("--no-summary", action="store_true", help="Disable executive Ollama summary")
        parser.add_argument("--llm-model", default="qwen2.5-coder:7b", help="Local LLM for executive summary")
        parser.add_argument("--no-wait", action="store_true", help="Do not wait for job completion")
        parser.add_argument("--close", action="store_true", help="Immediately shut down API after completion")
        cmd_ingest(parser.parse_args(args_to_parse))
        return

    if prog in ("media-status", "status"):
        parser = argparse.ArgumentParser(description="Inspect Media API job status")
        parser.add_argument("job_id", help="Job UUID to query")
        cmd_status(parser.parse_args(args_to_parse))
        return

    if prog in ("media-shutdown", "shutdown"):
        parser = argparse.ArgumentParser(description="Gracefully shut down Media API worker")
        parser.add_argument("--force", action="store_true", help="Force shutdown even if jobs are active")
        cmd_shutdown(parser.parse_args(args_to_parse))
        return

    if prog in ("media-update", "update"):
        parser = argparse.ArgumentParser(description="Check for and install yt-dlp extractor updates")
        parser.add_argument("--force", action="store_true", default=True, help="Force update check")
        cmd_update(parser.parse_args(args_to_parse))
        return

    if prog in ("media-api", "service"):
        parser = argparse.ArgumentParser(description="Manage Media API socket & service")
        parser.add_argument("action", nargs="?", default="status", choices=["status", "start", "stop", "restart", "logs", "standby", "update"], help="Action to perform")
        cmd_api(parser.parse_args(args_to_parse))
        return

    print("Usage: media-download | media-clip | media-transcribe | media-ingest | media-status | media-shutdown | media-update | media-api")

if __name__ == "__main__":
    main()
