#!/usr/bin/env python3
"""
Private terminal viewport for the sovereign spatial engine.

Renders corridor + agents + remittance pulses as ASCII on localhost only.
No browser required. No third-party game engine. Nothing leaves the box.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAME_PATH = ROOT / "state" / "spatial_frame.json"


def project(x: float, y: float, ox: float, oy: float, dx: float, dy: float, w: int, h: int) -> tuple[int, int]:
    # Map corridor bounding box into terminal cells
    min_x = min(ox, dx) - 50_000
    max_x = max(ox, dx) + 50_000
    min_y = min(oy, dy) - 50_000
    max_y = max(oy, dy) + 50_000
    if max_x == min_x:
        max_x = min_x + 1
    if max_y == min_y:
        max_y = min_y + 1
    col = int((x - min_x) / (max_x - min_x) * (w - 1))
    row = int((1.0 - (y - min_y) / (max_y - min_y)) * (h - 1))
    return max(0, min(w - 1, col)), max(0, min(h - 1, row))


def render(frame: dict, width: int, height: int) -> str:
    corridor = frame.get("corridor") or {}
    origin = (corridor.get("origin") or {}).get("xy") or [0.0, 0.0]
    dest = (corridor.get("destination") or {}).get("xy") or [1.0, 1.0]
    ox, oy = float(origin[0]), float(origin[1])
    dx, dy = float(dest[0]), float(dest[1])

    grid = [[" " for _ in range(width)] for _ in range(height)]

    # Origin O / Destination D
    oc, orow = project(ox, oy, ox, oy, dx, dy, width, height)
    dc, drow = project(dx, dy, ox, oy, dx, dy, width, height)
    grid[orow][oc] = "O"
    grid[drow][dc] = "D"

    # Corridor line
    steps = max(abs(dc - oc), abs(drow - orow), 1)
    for i in range(steps + 1):
        t = i / steps
        c = int(oc + (dc - oc) * t)
        r = int(orow + (drow - orow) * t)
        if grid[r][c] == " ":
            grid[r][c] = "·"

    for p in frame.get("capital_pulses") or []:
        c, r = project(float(p["x"]), float(p["y"]), ox, oy, dx, dy, width, height)
        if grid[r][c] not in ("O", "D"):
            grid[r][c] = "$"

    for a in frame.get("agents") or []:
        c, r = project(float(a["x"]), float(a["y"]), ox, oy, dx, dy, width, height)
        st = a.get("state") or ""
        if st == "REMITTING" or a.get("has_migrated"):
            ch = "R" if st == "REMITTING" else "A"
        elif st == "MIGRATING":
            ch = ">"
        elif st == "SETTLED":
            ch = "S"
        else:
            ch = "@"
        grid[r][c] = ch

    header = (
        f" SOVEREIGN VIEWPORT  tick={frame.get('tick')}  "
        f"agents={len(frame.get('agents') or [])}  "
        f"pulses={len(frame.get('capital_pulses') or [])}  "
        f"engine={frame.get('engine')} "
    )
    body = "\n".join("|" + "".join(row) + "|" for row in grid)
    return header + "\n" + "+" + "-" * width + "+\n" + body + "\n+" + "-" * width + "+"


def main() -> int:
    parser = argparse.ArgumentParser(description="Private ASCII spatial viewport")
    parser.add_argument("--hz", type=float, default=4.0)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=18)
    args = parser.parse_args()

    cols, _ = shutil.get_terminal_size((80, 24))
    width = args.width or max(40, min(cols - 4, 100))
    height = args.height
    dt = 1.0 / max(args.hz, 0.5)

    print("[viewport] private terminal surface — Ctrl+C to exit")
    try:
        while True:
            if FRAME_PATH.exists():
                try:
                    frame = json.loads(FRAME_PATH.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    frame = {"tick": 0, "agents": [], "capital_pulses": [], "engine": "waiting"}
            else:
                frame = {
                    "tick": 0,
                    "agents": [],
                    "capital_pulses": [],
                    "engine": "waiting",
                    "corridor": {
                        "origin": {"xy": [0, 0]},
                        "destination": {"xy": [1, 1]},
                    },
                }
            # Clear + redraw (ANSI) — stays in this TTY only
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write(render(frame, width, height) + "\n")
            sys.stdout.flush()
            time.sleep(dt)
    except KeyboardInterrupt:
        print("\n[viewport] closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
