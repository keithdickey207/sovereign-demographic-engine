#!/usr/bin/env python3
"""
Live sensor hub — builds a reality picture from whatever the host can sense.

Sources (all real where available; gracefully degrade):
  - WiFi heat: nmcli / iw / posted scans
  - Printers: lpstat / CUPS / Avahi / posted SNMP-like
  - Movement: spatial_frame agents + Quest IMU posts + PIR posts
  - RF / generic: HTTP ingest
  - Buildings: data/buildings.json

Emits fused field: state/reality_field.json for God Mode UI.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
DATA = ROOT / "data"
BUILDINGS_PATH = DATA / "buildings.json"
FRAME_PATH = STATE / "spatial_frame.json"
FIELD_PATH = STATE / "reality_field.json"
SENSOR_LOG = STATE / "sensor_events.jsonl"

HUB_HOST = os.environ.get("GODMODE_HOST", "0.0.0.0")
HUB_PORT = int(os.environ.get("GODMODE_PORT", "8770"))


def _now() -> float:
    return time.time()


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class SensorHub:
    """Thread-safe multi-sensor fusion for God Mode."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.buildings = self._load_buildings()
        self.wifi: list[dict[str, Any]] = []
        self.printers: list[dict[str, Any]] = []
        self.motion: list[dict[str, Any]] = []
        self.rf: list[dict[str, Any]] = []
        self.quest: list[dict[str, Any]] = []  # headset poses / controllers
        self.generic: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.heat_grid: list[dict[str, Any]] = []
        self.tick = 0
        self.started_at = _now()
        self.last_scan: dict[str, float] = {}
        STATE.mkdir(parents=True, exist_ok=True)

    def _load_buildings(self) -> dict[str, Any]:
        if BUILDINGS_PATH.exists():
            return json.loads(BUILDINGS_PATH.read_text(encoding="utf-8"))
        return {"buildings": [], "zones": [], "fixed_sensors": [], "origin": {}}

    # ── collectors ───────────────────────────────────────────────────

    def scan_wifi(self) -> list[dict[str, Any]]:
        """Real WiFi scan via NetworkManager if present."""
        out: list[dict[str, Any]] = []
        # nmcli device wifi list
        text = _run(["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,FREQ,CHAN,SECURITY", "dev", "wifi", "list"])
        if text.strip():
            for line in text.strip().splitlines():
                parts = line.split(":")
                # BSSID contains colons — nmcli -t escapes as \:
                # Use regex instead
            # better parse with fields that don't break
            text2 = _run(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "SSID,SIGNAL,FREQ,CHAN,SECURITY,BSSID",
                    "dev",
                    "wifi",
                    "list",
                ]
            )
            for line in text2.strip().splitlines():
                # last field is BSSID with colons escaped as \:
                m = re.match(
                    r"^(.*):(\d+):(\d+(?:\.\d+)?):(\d+):(.*):([0-9A-Fa-f\\:]+)$",
                    line,
                )
                if not m:
                    # fallback split from right
                    bits = line.split(":")
                    if len(bits) < 6:
                        continue
                    signal = bits[-5] if bits[-5].isdigit() else "0"
                    try:
                        sig = int(re.sub(r"\D", "", bits[1]) or "0")
                    except ValueError:
                        sig = 0
                    ssid = bits[0].replace("\\:", ":")
                    out.append(
                        {
                            "ssid": ssid or "(hidden)",
                            "signal": sig,
                            "freq_mhz": None,
                            "channel": None,
                            "security": "",
                            "bssid": "",
                            "source": "nmcli",
                            "ts": _now(),
                        }
                    )
                    continue
                ssid, signal, freq, chan, sec, bssid = m.groups()
                out.append(
                    {
                        "ssid": (ssid or "(hidden)").replace("\\:", ":"),
                        "signal": int(signal),
                        "freq_mhz": float(freq) if freq else None,
                        "channel": int(chan) if chan else None,
                        "security": sec.replace("\\:", ":"),
                        "bssid": bssid.replace("\\:", ":"),
                        "source": "nmcli",
                        "ts": _now(),
                    }
                )

        # iw dev scan dump (needs privileges often)
        if not out:
            text = _run(["iw", "dev"], timeout=2)
            ifaces = re.findall(r"Interface\s+(\S+)", text)
            for iface in ifaces[:2]:
                dump = _run(["iw", "dev", iface, "scan"], timeout=6)
                blocks = dump.split("BSS ")
                for b in blocks[1:]:
                    bssid_m = re.match(r"([0-9a-f:]{17})", b, re.I)
                    sig_m = re.search(r"signal:\s*([-\d.]+)", b)
                    ssid_m = re.search(r"SSID:\s*(.+)", b)
                    freq_m = re.search(r"freq:\s*(\d+)", b)
                    if not sig_m:
                        continue
                    # convert dBm ~ -30..-90 to 0..100
                    dbm = float(sig_m.group(1))
                    sig = int(_clamp((dbm + 90) / 60 * 100, 0, 100))
                    out.append(
                        {
                            "ssid": (ssid_m.group(1).strip() if ssid_m else "(hidden)"),
                            "signal": sig,
                            "dbm": dbm,
                            "freq_mhz": float(freq_m.group(1)) if freq_m else None,
                            "bssid": bssid_m.group(1) if bssid_m else "",
                            "source": f"iw:{iface}",
                            "ts": _now(),
                        }
                    )

        # ingest file posted by external scanners
        wifi_file = STATE / "sensors" / "wifi_scan.json"
        if wifi_file.exists():
            try:
                extra = json.loads(wifi_file.read_text(encoding="utf-8"))
                if isinstance(extra, list):
                    for e in extra:
                        e = dict(e)
                        e.setdefault("source", "file")
                        e.setdefault("ts", _now())
                        out.append(e)
            except (json.JSONDecodeError, OSError):
                pass

        # place APs on map using fixed_sensors + signal for heat
        placed = self._place_wifi(out)
        with self._lock:
            # Do not wipe live POSTed scans when OS scan is empty
            if placed:
                self.wifi = placed
            elif not self.wifi:
                self.wifi = placed
            self.last_scan["wifi"] = _now()
        return self.wifi

    def _place_wifi(self, scans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fixed = {
            s.get("ssid_hint", "").lower(): s
            for s in self.buildings.get("fixed_sensors") or []
            if s.get("kind") == "wifi_ap"
        }
        fixed_list = [s for s in self.buildings.get("fixed_sensors") or [] if s.get("kind") == "wifi_ap"]
        placed = []
        for i, ap in enumerate(scans):
            row = dict(ap)
            ssid = str(ap.get("ssid") or "").lower()
            match = None
            for hint, fs in fixed.items():
                if hint and hint.replace("sovereign-", "") in ssid.replace(" ", ""):
                    match = fs
                    break
            if match is None and fixed_list:
                match = fixed_list[i % len(fixed_list)]
            if match:
                row["x"] = match["x"]
                row["y"] = match["y"]
                row["z"] = match.get("z", 5)
                row["building"] = match.get("building")
            else:
                # ring layout for unknown APs
                ang = (i / max(len(scans), 1)) * math.tau
                r = 50 + (100 - float(ap.get("signal") or 50))
                row["x"] = math.cos(ang) * r
                row["y"] = math.sin(ang) * r
                row["z"] = 4.0
            row["heat"] = float(ap.get("signal") or 0) / 100.0
            placed.append(row)
        return placed

    def scan_printers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # CUPS
        text = _run(["lpstat", "-a"])
        for line in text.splitlines():
            # accepting requests since ...
            m = re.match(r"^(\S+)\s+accepting", line)
            if m:
                out.append(
                    {
                        "name": m.group(1),
                        "status": "accepting",
                        "source": "lpstat",
                        "ts": _now(),
                        "live": True,
                    }
                )
        text2 = _run(["lpstat", "-v"])
        for line in text2.splitlines():
            # device for NAME: uri
            m = re.match(r"device for (\S+):\s*(.+)", line)
            if m:
                name, uri = m.group(1), m.group(2).strip()
                found = next((p for p in out if p["name"] == name), None)
                if found:
                    found["uri"] = uri
                else:
                    out.append(
                        {
                            "name": name,
                            "uri": uri,
                            "source": "lpstat",
                            "ts": _now(),
                            "live": True,
                        }
                    )

        # Avahi/Bonjour printers
        avahi = _run(["avahi-browse", "-art"], timeout=5)
        for line in avahi.splitlines():
            if "ipp" in line.lower() or "printer" in line.lower():
                out.append(
                    {
                        "name": line.strip()[:120],
                        "source": "avahi",
                        "ts": _now(),
                        "live": True,
                    }
                )

        # posted printer telemetry (page counts, jobs)
        pr_file = STATE / "sensors" / "printers.json"
        if pr_file.exists():
            try:
                extra = json.loads(pr_file.read_text(encoding="utf-8"))
                if isinstance(extra, list):
                    out.extend(extra)
            except (json.JSONDecodeError, OSError):
                pass

        # place on map
        fixed = [s for s in self.buildings.get("fixed_sensors") or [] if s.get("kind") == "printer"]
        for i, p in enumerate(out):
            if "x" not in p and fixed:
                fs = fixed[i % len(fixed)]
                p["x"], p["y"], p["z"] = fs["x"], fs["y"], fs.get("z", 1.1)
                p["building"] = fs.get("building")
            p.setdefault("activity", 0.3)
            # pulse activity if recently seen
            age = _now() - float(p.get("ts") or _now())
            p["activity"] = _clamp(1.0 - age / 120.0, 0.1, 1.0)
            if p.get("jobs"):
                p["activity"] = _clamp(0.4 + 0.1 * int(p["jobs"]), 0.0, 1.0)

        with self._lock:
            if out:
                # merge with any POSTed printers not in CUPS
                by_name = {p.get("name"): p for p in out if p.get("name")}
                for p in self.printers:
                    n = p.get("name")
                    if n and n not in by_name:
                        by_name[n] = p
                self.printers = list(by_name.values()) if by_name else out
            self.last_scan["printers"] = _now()
        return self.printers

    def scan_movement(self) -> list[dict[str, Any]]:
        """Movement from spatial agents + motion sensors + quest poses."""
        events: list[dict[str, Any]] = []
        if FRAME_PATH.exists():
            try:
                frame = json.loads(FRAME_PATH.read_text(encoding="utf-8"))
                for a in frame.get("agents") or []:
                    # map lat/lon offset into local meters-ish for godmode overlay
                    # use engine x,y if present
                    events.append(
                        {
                            "id": a.get("id"),
                            "kind": "agent",
                            "name": a.get("name") or a.get("id"),
                            "state": a.get("state"),
                            "x": float(a.get("x") or 0) / 5000.0,  # compress corridor into campus
                            "y": float(a.get("y") or 0) / 5000.0,
                            "z": 1.7,
                            "vx": float(a.get("vx") or 0) / 5000.0,
                            "vy": float(a.get("vy") or 0) / 5000.0,
                            "speed": math.hypot(float(a.get("vx") or 0), float(a.get("vy") or 0)),
                            "ts": _now(),
                            "source": "spatial_frame",
                        }
                    )
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass

        # fixed PIR sensors — if no live posts, show idle
        for s in self.buildings.get("fixed_sensors") or []:
            if s.get("kind") != "motion":
                continue
            # look for recent motion events near sensor
            recent = [
                m
                for m in self.motion
                if m.get("sensor_id") == s["id"] and _now() - float(m.get("ts") or 0) < 15
            ]
            events.append(
                {
                    "id": s["id"],
                    "kind": "pir",
                    "name": s.get("name") or s["id"],
                    "x": s["x"],
                    "y": s["y"],
                    "z": s.get("z", 2.5),
                    "active": bool(recent),
                    "intensity": 1.0 if recent else 0.05,
                    "ts": _now(),
                    "source": "pir",
                }
            )

        with self._lock:
            # keep quest headsets as motion sources
            for q in self.quest:
                if _now() - float(q.get("ts") or 0) < 5:
                    events.append(
                        {
                            "id": q.get("id") or "quest",
                            "kind": "quest",
                            "name": q.get("name") or "Meta Quest",
                            "x": float(q.get("x") or 0),
                            "y": float(q.get("y") or 0),
                            "z": float(q.get("z") or 1.6),
                            "yaw": q.get("yaw"),
                            "active": True,
                            "intensity": 1.0,
                            "ts": q.get("ts"),
                            "source": "quest",
                        }
                    )
            self.agents = [e for e in events if e.get("kind") == "agent"]
            # store combined movement snapshot separately in field builder
            self.last_scan["movement"] = _now()
            self._movement_snapshot = events
        return events

    def ingest(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept live sensor posts from Quest, ESP32, printers, phones, etc."""
        row = dict(payload)
        row["ts"] = float(row.get("ts") or _now())
        row["kind"] = kind
        with self._lock:
            if kind in ("wifi", "wifi_scan"):
                scans = payload.get("aps") or payload.get("networks") or [payload]
                if isinstance(scans, dict):
                    scans = [scans]
                # drop bare kind-only shells
                scans = [s for s in scans if isinstance(s, dict) and (s.get("ssid") or s.get("signal"))]
                placed = self._place_wifi(list(scans))
                # merge by bssid/ssid
                by_key = {}
                for ap in self.wifi + placed:
                    key = str(ap.get("bssid") or ap.get("ssid") or id(ap))
                    by_key[key] = ap
                self.wifi = list(by_key.values())
                self.last_scan["wifi"] = _now()
            elif kind in ("printer", "printers"):
                items = payload.get("printers") or [payload]
                if isinstance(items, dict):
                    items = [items]
                fixed = [s for s in self.buildings.get("fixed_sensors") or [] if s.get("kind") == "printer"]
                by_name = {p.get("name"): p for p in self.printers if p.get("name")}
                for i, p in enumerate(items):
                    if not isinstance(p, dict):
                        continue
                    p = dict(p)
                    p.setdefault("ts", _now())
                    p["live"] = True
                    name = p.get("name") or f"prt_{len(by_name)}"
                    p["name"] = name
                    if "x" not in p and fixed:
                        fs = fixed[i % len(fixed)]
                        p["x"], p["y"], p["z"] = fs["x"], fs["y"], fs.get("z", 1.1)
                        p["building"] = fs.get("building")
                    p["activity"] = float(p.get("activity") or (0.5 + 0.1 * int(p.get("jobs") or 0)))
                    by_name[name] = p
                self.printers = list(by_name.values())
                self.last_scan["printers"] = _now()
            elif kind in ("motion", "pir"):
                self.motion.append(row)
                self.motion = self.motion[-200:]
            elif kind in ("quest", "headset", "imu"):
                # replace same headset id
                hid = row.get("id") or row.get("device_id") or "quest-1"
                row["id"] = hid
                self.quest = [q for q in self.quest if q.get("id") != hid] + [row]
            elif kind in ("rf", "sdr", "spectrum"):
                self.rf.append(row)
                self.rf = self.rf[-100:]
            else:
                self.generic.append(row)
                self.generic = self.generic[-200:]
        self._log_event(kind, row)
        return {"ok": True, "kind": kind, "ts": row["ts"]}

    def _log_event(self, kind: str, row: dict[str, Any]) -> None:
        try:
            with SENSOR_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": kind, "ts": _iso(), "data": row}, default=str) + "\n")
        except OSError:
            pass

    # ── fusion ───────────────────────────────────────────────────────

    def fuse_heat_grid(self, step: float = 8.0, extent: float = 160.0) -> list[dict[str, Any]]:
        """2D heat field from wifi + motion + printers + rf."""
        cells: dict[tuple[int, int], dict[str, float]] = {}

        def add(x: float, y: float, wifi: float = 0, motion: float = 0, print_a: float = 0, rf: float = 0):
            cx = int(round(x / step))
            cy = int(round(y / step))
            key = (cx, cy)
            c = cells.setdefault(key, {"wifi": 0.0, "motion": 0.0, "print": 0.0, "rf": 0.0, "n": 0})
            c["wifi"] = max(c["wifi"], wifi)
            c["motion"] = max(c["motion"], motion)
            c["print"] = max(c["print"], print_a)
            c["rf"] = max(c["rf"], rf)
            c["n"] += 1
            # spread to neighbors (soft heat)
            for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                k2 = (cx + dx, cy + dy)
                c2 = cells.setdefault(k2, {"wifi": 0.0, "motion": 0.0, "print": 0.0, "rf": 0.0, "n": 0})
                fall = 0.55 if (dx or dy) else 1.0
                c2["wifi"] = max(c2["wifi"], wifi * fall)
                c2["motion"] = max(c2["motion"], motion * fall)
                c2["print"] = max(c2["print"], print_a * fall)
                c2["rf"] = max(c2["rf"], rf * fall)

        with self._lock:
            wifi = list(self.wifi)
            printers = list(self.printers)
            motion_events = list(getattr(self, "_movement_snapshot", []) or [])
            rf = list(self.rf)

        for ap in wifi:
            add(float(ap.get("x") or 0), float(ap.get("y") or 0), wifi=float(ap.get("heat") or 0))
        for p in printers:
            add(float(p.get("x") or 0), float(p.get("y") or 0), print_a=float(p.get("activity") or 0.2))
        for m in motion_events:
            inten = float(m.get("intensity") or (0.8 if m.get("active") else 0.1))
            if m.get("kind") == "agent":
                inten = _clamp(0.2 + float(m.get("speed") or 0) / 50000.0, 0.15, 1.0)
            add(float(m.get("x") or 0), float(m.get("y") or 0), motion=inten)
        for r in rf:
            add(float(r.get("x") or 0), float(r.get("y") or 0), rf=float(r.get("power") or 0.5))

        grid = []
        for (cx, cy), c in cells.items():
            x, y = cx * step, cy * step
            if abs(x) > extent or abs(y) > extent:
                continue
            total = 0.4 * c["wifi"] + 0.3 * c["motion"] + 0.2 * c["print"] + 0.1 * c["rf"]
            grid.append(
                {
                    "x": x,
                    "y": y,
                    "wifi": round(c["wifi"], 3),
                    "motion": round(c["motion"], 3),
                    "print": round(c["print"], 3),
                    "rf": round(c["rf"], 3),
                    "total": round(_clamp(total, 0.0, 1.0), 3),
                }
            )
        with self._lock:
            self.heat_grid = grid
        return grid

    def tick_once(self) -> dict[str, Any]:
        self.scan_wifi()
        self.scan_printers()
        movement = self.scan_movement()
        heat = self.fuse_heat_grid()
        with self._lock:
            self.tick += 1
            field = {
                "engine": "sovereign-godmode",
                "mode": "god",
                "tick": self.tick,
                "ts": _iso(),
                "uptime_s": round(_now() - self.started_at, 1),
                "origin": self.buildings.get("origin"),
                "buildings": self.buildings.get("buildings") or [],
                "zones": self.buildings.get("zones") or [],
                "fixed_sensors": self.buildings.get("fixed_sensors") or [],
                "wifi": self.wifi,
                "printers": self.printers,
                "movement": movement,
                "rf": self.rf[-50:],
                "quest": self.quest,
                "heat": heat,
                "last_scan": dict(self.last_scan),
                "layers": {
                    "wifi_heat": True,
                    "motion": True,
                    "printers": True,
                    "buildings": True,
                    "agents": True,
                    "quest": True,
                },
            }
        tmp = FIELD_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(field, indent=2), encoding="utf-8")
        tmp.replace(FIELD_PATH)
        return field

    def snapshot(self) -> dict[str, Any]:
        if FIELD_PATH.exists():
            try:
                return json.loads(FIELD_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return self.tick_once()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ── HTTP API for Quest + sensors ─────────────────────────────────────

_hub: SensorHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> SensorHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = SensorHub()
        return _hub


class HubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[sensor-hub] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        hub = get_hub()
        if path in ("/", "/health"):
            self._json(200, {"status": "ok", "service": "sensor-hub", "godmode": True})
            return
        if path in ("/api/field", "/api/reality"):
            self._json(200, hub.snapshot())
            return
        if path == "/api/wifi":
            self._json(200, hub.wifi)
            return
        if path == "/api/printers":
            self._json(200, hub.printers)
            return
        if path == "/api/movement":
            self._json(200, getattr(hub, "_movement_snapshot", []))
            return
        if path == "/api/scan":
            self._json(200, hub.tick_once())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        hub = get_hub()
        if path.startswith("/api/ingest/"):
            kind = path.split("/api/ingest/", 1)[-1] or "generic"
            self._json(200, hub.ingest(kind, body if isinstance(body, dict) else {"value": body}))
            return
        if path == "/api/quest":
            self._json(200, hub.ingest("quest", body))
            return
        if path == "/api/ingest":
            kind = str(body.get("kind") or "generic")
            self._json(200, hub.ingest(kind, body))
            return
        self._json(404, {"error": "not found"})


def run_poll_loop(hub: SensorHub, interval: float = 2.0) -> None:
    while True:
        try:
            hub.tick_once()
        except Exception as exc:
            print(f"[sensor-hub] tick error: {exc}")
        time.sleep(interval)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sensor hub for God Mode")
    parser.add_argument("--host", default=HUB_HOST)
    parser.add_argument("--port", type=int, default=HUB_PORT)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    hub = get_hub()
    field = hub.tick_once()
    print(
        f"[sensor-hub] wifi={len(field.get('wifi') or [])} "
        f"printers={len(field.get('printers') or [])} "
        f"movement={len(field.get('movement') or [])} "
        f"heat_cells={len(field.get('heat') or [])}"
    )
    if args.once:
        print(f"[sensor-hub] wrote {FIELD_PATH}")
        return 0

    t = threading.Thread(target=run_poll_loop, args=(hub, args.interval), daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((args.host, args.port), HubHandler)
    print(f"[sensor-hub] API http://{args.host}:{args.port}  field→{FIELD_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[sensor-hub] stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
