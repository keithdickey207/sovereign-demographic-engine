#!/usr/bin/env python3
"""
God Mode server — UI for desktop + Meta Quest Browser (WebXR).

Serves:
  - /              God Mode 3D interface
  - /api/*         reality field + sensor proxy
  - static assets

Quest usage:
  1. Start this server on a machine your Quest can reach (same WiFi)
  2. In Quest Browser open http://<host-ip>:8771/
  3. Tap ENTER VR / GOD MODE
  4. Optional: Quest pose posts to /api/quest automatically from the page
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layer5_godmode.sensor_hub import SensorHub, get_hub, run_poll_loop  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
STATE = ROOT / "state"
FRAME_PATH = STATE / "spatial_frame.json"

HOST = os.environ.get("GODMODE_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("GODMODE_UI_PORT", "8771"))


class GodModeHandler(BaseHTTPRequestHandler):
    hub: SensorHub

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[godmode] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        # Quest browser caches aggressively — bust for live field
        if "json" in content_type:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._bytes(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/godmode"):
            html = (STATIC / "godmode.html").read_bytes()
            self._bytes(200, html, "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            fp = (STATIC / rel).resolve()
            if not str(fp).startswith(str(STATIC.resolve())) or not fp.exists():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            self._bytes(200, fp.read_bytes(), ctype)
            return
        if path in ("/health", "/api/health"):
            self._json(200, {"status": "ok", "service": "godmode", "quest": True})
            return
        if path in ("/api/field", "/api/reality"):
            self._json(200, self.hub.snapshot())
            return
        if path == "/api/scan":
            self._json(200, self.hub.tick_once())
            return
        if path == "/api/spatial":
            if FRAME_PATH.exists():
                try:
                    self._json(200, json.loads(FRAME_PATH.read_text(encoding="utf-8")))
                    return
                except (json.JSONDecodeError, OSError):
                    pass
            self._json(200, {"agents": [], "engine": "offline"})
            return
        if path == "/api/wifi":
            self._json(200, self.hub.wifi)
            return
        if path == "/api/printers":
            self._json(200, self.hub.printers)
            return
        self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if path.startswith("/api/ingest/"):
            kind = path.split("/api/ingest/", 1)[-1]
            self._json(200, self.hub.ingest(kind, body if isinstance(body, dict) else {}))
            return
        if path in ("/api/quest", "/api/ingest"):
            kind = str(body.get("kind") or "quest")
            self._json(200, self.hub.ingest(kind, body if isinstance(body, dict) else {}))
            return
        self._json(404, {"error": "not found"})


def main() -> int:
    import argparse
    import socket

    parser = argparse.ArgumentParser(description="Sovereign God Mode UI + WebXR")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--interval", type=float, default=1.5)
    args = parser.parse_args()

    STATIC.mkdir(parents=True, exist_ok=True)
    hub = get_hub()
    GodModeHandler.hub = hub
    hub.tick_once()

    poller = threading.Thread(target=run_poll_loop, args=(hub, args.interval), daemon=True)
    poller.start()

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), GodModeHandler)
    except OSError as exc:
        if getattr(exc, "errno", None) == 98:  # Address already in use
            print(
                f"[godmode] port {args.port} already in use.\n"
                f"  Free it:  kill $(ss -tlnp | awk '/:{args.port}/{{print}}' | "
                f"grep -oP 'pid=\\K[0-9]+' | head -1)\n"
                f"  Or:       fuser -k {args.port}/tcp\n"
                f"  Or other: python scripts/run_godmode.py --port 8772"
            )
            return 1
        raise

    # print LAN URLs for Quest
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips = sorted(set(ips))

    print("[godmode] SOVEREIGN GOD MODE online")
    print(f"[godmode] local:  http://127.0.0.1:{args.port}/")
    for ip in ips:
        print(f"[godmode] Quest:  http://{ip}:{args.port}/   ← open in Meta Quest Browser")
    print("[godmode] layers: buildings · wifi heat · printers · motion · quest · agents")
    print("[godmode] POST live sensors → /api/ingest/<kind> or /api/quest")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[godmode] shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
