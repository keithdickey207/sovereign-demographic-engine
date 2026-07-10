#!/usr/bin/env python3
"""
One-command offline prototype:

  schema → resolve → debug(fix) → pattern-aware inference tick → spatial --proto

No Ollama required. No Neo4j required. No game engine.
Pattern memory evolves; debug agent keeps state clean.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str]) -> int:
    print(f"\n[proto] $ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sovereign demographic prototype")
    parser.add_argument("--skip-resolve", action="store_true")
    parser.add_argument("--skip-debug", action="store_true")
    parser.add_argument("--with-ollama", action="store_true", help="Use local Ollama on inference tick")
    parser.add_argument("--model", default="llama3.2")
    parser.add_argument(
        "--episodes",
        type=int,
        default=1,
        help="How many spatial corridor runs (learning stacks across episodes)",
    )
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

    if not args.skip_debug:
        rc = run(
            [
                py,
                str(ROOT / "bridge" / "debug_agent.py"),
                "--fix",
                "--sync-docs",
            ]
        )
        # non-zero = unhealthy; still continue if only warnings after fix
        # but abort on residual criticals: re-check by reading report
        if rc not in (0, 1):
            return rc

    infer = [
        py,
        str(ROOT / "layer3_inference" / "decision_daemon.py"),
        "--once",
        "--no-api",
        "--model",
        args.model,
    ]
    if not args.with_ollama:
        infer.append("--no-ollama")
    rc = run(infer)
    if rc != 0:
        return rc

    for ep in range(1, max(1, args.episodes) + 1):
        print(f"\n[proto] ══ spatial episode {ep}/{args.episodes} ══")
        rc = run(
            [
                py,
                str(ROOT / "layer4_spatial" / "spatial_engine.py"),
                "--proto",
                "--no-api",
            ]
        )
        if rc != 0:
            return rc

    # Final health snapshot after learning
    run([py, str(ROOT / "bridge" / "debug_agent.py"), "--sync-docs"])
    run([py, str(ROOT / "scripts" / "show_results.py"), "--html"])
    print("\n[proto] done")
    print("[proto] SEE RESULTS:")
    print(f"  cat {ROOT / 'state' / 'RESULTS.txt'}")
    print(f"  cat {ROOT / 'state' / 'RESULTS.md'}")
    print(f"  xdg-open {ROOT / 'state' / 'results.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
