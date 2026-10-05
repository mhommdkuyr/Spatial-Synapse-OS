#!/usr/bin/env python3
import json
import os
import socketserver
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "10000"))
STATUS = ROOT / "data" / "runner_status.json"


def run_scan():
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATUS.write_text(json.dumps({"status": "running"}, indent=2), encoding="utf-8")
        proc = subprocess.run(
            ["python", "scripts/ibb_scan.py", "--config", "config/ibb.yml"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        status = {
            "status": "completed" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
        STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        STATUS.write_text(
            json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    threading.Thread(target=run_scan, daemon=True).start()
    os.chdir(ROOT)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), QuietHandler) as httpd:
        httpd.serve_forever()
