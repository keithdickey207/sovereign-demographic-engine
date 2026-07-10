#!/usr/bin/env python3
"""
Train & run protection-oriented watch agents.

Agents learn to flag vulnerable people/groups and exploitation-network patterns
for prevention — not to stalk or harm individuals.

Examples:
  cd ~/projects/sovereign-demographic-engine
  source .venv/bin/activate

  # Train agents (synthetic + your resolved persons)
  python scripts/train_watch_agents.py --train

  # Scan people + known high-risk places
  python scripts/train_watch_agents.py --scan

  # Train then scan
  python scripts/train_watch_agents.py --train --scan --epochs 12

  # List agent profiles
  python scripts/train_watch_agents.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.watch_agents import (  # noqa: E402
    MODEL_PATH,
    HITS_PATH,
    REPORT_MD,
    WatchAgentTrainer,
    load_real_people,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train/run watch agents")
    parser.add_argument("--train", action="store_true", help="Train agents")
    parser.add_argument("--scan", action="store_true", help="Scan people + places")
    parser.add_argument("--list", action="store_true", help="List agent profiles")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--synthetic", type=int, default=120, help="Synthetic training people")
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not any([args.train, args.scan, args.list]):
        args.train = True
        args.scan = True

    trainer = WatchAgentTrainer()

    if args.list:
        print(f"{'Profile':40} {'Category':18} {'Priority':10} thr")
        print("-" * 80)
        for p in trainer.profiles:
            a = trainer.model["agents"].get(p["id"], {})
            print(
                f"{p['label'][:40]:40} {p['category'][:18]:18} {p['priority'][:10]:10} "
                f"{float(a.get('threshold', 0.55)):.2f}"
            )
        print(f"\nModel: {MODEL_PATH}")
        return 0

    if args.train:
        real = load_real_people()
        print(f"[watch] training epochs={args.epochs} synthetic={args.synthetic} real={len(real)}")
        result = trainer.train(
            epochs=args.epochs,
            synthetic=args.synthetic,
            real_people=real,
        )
        print(f"[watch] trained {result['agents']} agents on ~{result['samples']} samples")
        for pid, info in result["per_agent"].items():
            label = trainer.model["agents"][pid]["label"]
            print(
                f"  {label[:34]:34} acc={info['accuracy_ema']:.2f} "
                f"thr={info['threshold']:.2f} n={info['samples_seen']}"
            )
        print(f"[watch] model → {MODEL_PATH}")

    if args.scan:
        real = load_real_people()
        if not real:
            print("[watch] no resolved_persons.json — scanning synthetic demo cohort")
            real = trainer._synthetic_people(24)
        people_hits = trainer.scan_people(real, min_score=args.min_score)
        place_hits = trainer.scan_places()
        trainer.save_hits(people_hits, place_hits)

        print()
        print("=" * 72)
        print("  WATCH HITS — protection triage (not criminal charges)")
        print("=" * 72)
        print(f"  People flagged: {len(people_hits)} / {len(real)}")
        for h in people_hits[:15]:
            m = h["matches"][0]
            print(f"  • {h['name']}: {m['label']}  score={m['score']:.2f}  [{m['priority']}]")
            print(f"      alerts: {', '.join(m.get('alerts') or [])}")
            print(f"      → {m.get('response')}")
        print()
        print(f"  Places / networks flagged: {len(place_hits)}")
        for h in place_hits[:12]:
            m = h["matches"][0]
            print(
                f"  • {h['name']} ({h.get('country')}) known={h.get('known_involvement')} "
                f"— {m['label']} score={m['score']:.2f}"
            )
        print()
        print(f"  Report: {REPORT_MD}")
        print(f"  JSON:   {HITS_PATH}")
        print("=" * 72)

        if args.json:
            print(json.dumps({"people_hits": people_hits, "place_hits": place_hits}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
