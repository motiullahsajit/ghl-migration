#!/usr/bin/env python3
"""Local HTML dashboard for migration progress."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from migration.registry import MigrationRegistry

DASHBOARD_DIR = ROOT / "dashboard"


class Handler(BaseHTTPRequestHandler):
    registry: MigrationRegistry

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, data: object, code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        run_id = (qs.get("run_id") or [""])[0]

        if parsed.path == "/api/status":
            if not run_id:
                self._json({"error": "run_id required"}, 400)
                return
            self._json(self.registry.export_summary(run_id))
            return
        if parsed.path == "/api/failures":
            limit = int((qs.get("limit") or ["50"])[0])
            self._json(self.registry.recent_failures(run_id, limit=limit))
            return
        if parsed.path == "/api/activity":
            limit = int((qs.get("limit") or ["20"])[0])
            self._json(self.registry.recent_events(run_id, limit=limit))
            return

        if parsed.path in ("/", "/index.html"):
            self._file(DASHBOARD_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self._file(DASHBOARD_DIR / "styles.css", "text/css")
            return
        if parsed.path == "/app.js":
            self._file(DASHBOARD_DIR / "app.js", "application/javascript")
            return
        self.send_error(404)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="")
    parser.add_argument("--db", default=str(ROOT / "data" / "migration.db"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8765")))
    parser.add_argument("--no-open-browser", action="store_true")
    args = parser.parse_args()

    registry = MigrationRegistry(Path(args.db))
    Handler.registry = registry

    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://{host}:{args.port}/"
    if args.run_id:
        url += f"?run_id={args.run_id}"
    print(f"Dashboard: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
