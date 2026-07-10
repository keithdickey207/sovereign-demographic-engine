#!/usr/bin/env python3
"""
One-shot pipeline: schema → resolve → inference → custom spatial engine.

No third-party game runtimes. Spatial layer is sovereign-spatial only.

Usage:
  .venv/bin/python bridge/tick_orchestrator.py
  .venv/bin/python bridge/tick_orchestrator.py --full
  .venv/bin/python bridge/tick_orchestrator.py --daemon
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str]) -> int:
    print(f"[orch] $ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="Start inference daemon after seed")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Seed + one inference tick + short spatial run (private engine)",
    )
    parser.add_argument("--model", default="llama3.2")
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--skip-resolve", action="store_true")
    parser.add_argument("--spatial-seconds", type=float, default=12.0)
    args = parser.parse_args()

    py = str(ROOT / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable

    rc = run([py, str(ROOT / "layer1_entity" / "apply_schema.py")])
    if rc != 0:
        return rc

    if not args.skip_resolve:
        rc = run([py, str(ROOT / "layer2_ingest" / "resolve_entities.py")])
        if rc != 0:
            return rc

    if args.daemon:
        return run(
            [
                py,
                str(ROOT / "layer3_inference" / "decision_daemon.py"),
                "--model",
                args.model,
                "--interval",
                str(args.interval),
            ]
        )

    rc = run(
        [
            py,
            str(ROOT / "layer3_inference" / "decision_daemon.py"),
            "--model",
            args.model,
            "--once",
            "--no-api",
        ]
    )
    if rc != 0:
        return rc

    if args.full:
        # Prefer clean prototype path (reset + learn + until-settled)
        return run(
            [
                py,
                str(ROOT / "layer4_spatial" / "spatial_engine.py"),
                "--proto",
                "--no-api",
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
