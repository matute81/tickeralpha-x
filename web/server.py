"""Local TickerAlpha briefs app. Secrets stay in .env — never in the browser."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import generate_pack  # noqa: E402
from fetch_fmp_brief import load_dotenv  # noqa: E402
from llm_ideas import (  # noqa: E402
    llm_configured,
    read_context,
    read_focus,
    read_full_prompt,
    reset_focus,
    reset_full_prompt,
    unavailable_message,
    write_context,
    write_focus,
    write_full_prompt,
)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
MODES = {"morning", "close", "week-ahead"}


def _prompt_mode(path: str) -> str:
    """morning from /api/prompts/focus/morning or legacy /api/prompts/morning."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name


def _start_job(mode: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {"id": job_id, "mode": mode, "status": "running", "error": None, "result": None}

    def worker() -> None:
        try:
            import importlib

            import fetch_fmp_brief
            import llm_ideas
            importlib.reload(fetch_fmp_brief)
            importlib.reload(llm_ideas)
            importlib.reload(generate_pack)
            result = generate_pack.run_job(mode)
            with LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except Exception as exc:  # noqa: BLE001 — surface any job failure to the UI
            with LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = str(exc)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            load_dotenv(ROOT / ".env")
            self._json(
                200,
                {
                    "ok": True,
                    "fmp": bool((os.environ.get("FMP_API_KEY") or "").strip()),
                    "llm": bool(
                        (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
                    ),
                },
            )
            return
        if path == "/api/context":
            self._json(200, read_context())
            return
        if path == "/api/prompts/full":
            try:
                text, customized = read_full_prompt()
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"text": text, "customized": customized})
            return
        if path.startswith("/api/prompts/"):
            mode = _prompt_mode(path)
            if mode not in MODES:
                self._json(400, {"error": "mode must be morning, close, or week-ahead."})
                return
            try:
                text, customized = read_focus(mode)
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"mode": mode, "text": text, "customized": customized})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._json(404, {"error": "Job not found."})
                return
            self._json(200, job)
            return
        if path in {"/", "/index.html"}:
            html = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/jobs":
            self._json(404, {"error": "Not found."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Invalid JSON."})
            return
        mode = str(body.get("mode") or "")
        if mode not in MODES:
            self._json(400, {"error": "mode must be morning, close, or week-ahead."})
            return
        load_dotenv(ROOT / ".env")
        if not (os.environ.get("FMP_API_KEY") or "").strip():
            self._json(400, {"error": "FMP_API_KEY is missing from .env."})
            return
        if not llm_configured():
            self._json(400, {"error": unavailable_message()})
            return
        job_id = _start_job(mode)
        self._json(202, {"id": job_id, "status": "running"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 400_000:
            raise ValueError("Body too large.")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        if path == "/api/context":
            try:
                saved = write_context(body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True, **saved})
            return
        if path == "/api/prompts/full":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._json(400, {"error": "text is required."})
                return
            try:
                write_full_prompt(text)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True, "customized": True})
            return
        if path.startswith("/api/prompts/"):
            mode = _prompt_mode(path)
            if mode not in MODES:
                self._json(400, {"error": "mode must be morning, close, or week-ahead."})
                return
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._json(400, {"error": "text is required."})
                return
            try:
                write_focus(mode, text)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"mode": mode, "ok": True, "customized": True})
            return
        self._json(404, {"error": "Not found."})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/context":
            saved = write_context({})
            self._json(200, {"ok": True, **saved})
            return
        if path == "/api/prompts/full":
            try:
                reset_full_prompt()
                text, customized = read_full_prompt()
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"ok": True, "customized": customized, "text": text})
            return
        if path.startswith("/api/prompts/"):
            mode = _prompt_mode(path)
            if mode not in MODES:
                self._json(400, {"error": "mode must be morning, close, or week-ahead."})
                return
            reset_focus(mode)
            text, customized = read_focus(mode)
            self._json(200, {"mode": mode, "ok": True, "customized": customized, "text": text})
            return
        self._json(404, {"error": "Not found."})


def main() -> None:
    load_dotenv(ROOT / ".env")
    host = os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("PORT") or 8787)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.allow_reuse_address = False
    print(f"TickerAlpha briefs -> http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
