#!/usr/bin/env python3
"""
Global trafficking RISK scanner — prioritizes known high-involvement areas.

Prevention / research only. Not proof of crime. Not how-to guidance.

Examples:
  cd ~/projects/sovereign-demographic-engine
  source .venv/bin/activate

  # Rank known high-involvement hotspots worldwide
  python scripts/risk_scan.py --hotspots
  python scripts/risk_scan.py --hotspots --kind sex
  python scripts/risk_scan.py --hotspots --kind labor

  # Scan a place you care about
  python scripts/risk_scan.py --area "Tenancingo"
  python scripts/risk_scan.py --area "El Paso"
  python scripts/risk_scan.py --area "Benin City"
  python scripts/risk_scan.py --lat 13.75 --lon 100.50   # Bangkok area

  # List all covered areas
  python scripts/risk_scan.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.trafficking_risk import (  # noqa: E402
    TraffickingRiskEngine,
    format_report_md,
    format_report_text,
)


def print_hotspots(rows: list[dict], kind: str) -> None:
    print("=" * 72)
    print(f"  KNOWN HIGH-INVOLVEMENT HOTSPOTS  (kind={kind})")
    print("  Prevention risk screen — not a charge sheet")
    print("=" * 72)
    print(
        f"  {'#':>3}  {'Area':22} {'Country':14} {'Overall':>7} {'Sex':>5} {'Labor':>5}  Tag"
    )
    print("  " + "-" * 68)
    for r in rows:
        print(
            f"  {r['rank']:3d}  {r['name'][:22]:22} {r['country'][:14]:14} "
            f"{r['overall_0_100']:7.1f} {r['sex_trafficking_risk_0_100']:5.1f} "
            f"{r['labor_trafficking_risk_0_100']:5.1f}  {r['known_involvement']}"
        )
        types = ", ".join(r.get("involvement_types") or [])
        if types:
            print(f"       types: {types}")
        if r.get("top_corridor"):
            print(f"       corridor: {r['top_corridor']}")
    print()
    print(f"  Saved reports: state/RISK_REPORT.md  state/risk_report.json")
    print("  Expand coverage: data/global_nodes.json + data/global_corridors.json")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Global trafficking RISK scan (known high-involvement focus)"
    )
    parser.add_argument("--area", type=str, help="Place name / country / region")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument(
        "--hotspots",
        action="store_true",
        help="List ranked known high-involvement areas worldwide",
    )
    parser.add_argument(
        "--kind",
        choices=["all", "sex", "labor", "child"],
        default="all",
        help="Hotspot ranking metric",
    )
    parser.add_argument(
        "--all-nodes",
        action="store_true",
        help="With --hotspots, include watchlist nodes too",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--list", action="store_true", help="List covered areas")
    parser.add_argument("--json", action="store_true", help="Print raw JSON")
    args = parser.parse_args()

    eng = TraffickingRiskEngine()

    if args.list:
        for a in eng.list_areas():
            node = eng._by_id.get(a["id"], {})
            tag = node.get("known_involvement", "watch")
            print(f"{a['name']:28} {a['country']:16} {tag:16} {a['region']}")
        print(f"\n{len(eng.nodes)} areas · {len(eng.corridors)} corridors")
        return 0

    if args.hotspots or (
        not args.area and args.lat is None and args.lon is None
    ):
        rows = eng.rank_hotspots(
            kind=args.kind,
            known_only=not args.all_nodes,
            limit=args.limit,
        )
        payload = {
            "ok": True,
            "mode": "hotspots",
            "kind": args.kind,
            "count": len(rows),
            "hotspots": rows,
            "disclaimer": eng.disclaimer(),
        }
        eng.save_report(payload)
        # also write a readable md for hotspots
        lines = [
            "# Known high-involvement trafficking RISK hotspots",
            "",
            "> " + eng.disclaimer(),
            "",
            f"Kind: `{args.kind}` · count: {len(rows)}",
            "",
            "| Rank | Area | Country | Overall | Sex | Labor | Child | Tag |",
            "|-----:|------|---------|--------:|----:|------:|------:|-----|",
        ]
        for r in rows:
            lines.append(
                f"| {r['rank']} | {r['name']} | {r['country']} | "
                f"{r['overall_0_100']} | {r['sex_trafficking_risk_0_100']} | "
                f"{r['labor_trafficking_risk_0_100']} | "
                f"{r['child_exploitation_risk_0_100']} | {r['known_involvement']} |"
            )
        lines += ["", "## Notes", ""]
        for r in rows[:10]:
            lines.append(f"- **{r['name']}**: {r.get('notes')}")
        (ROOT / "state" / "RISK_REPORT.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_hotspots(rows, args.kind)
        return 0

    report = eng.scan_area(query=args.area, lat=args.lat, lon=args.lon, top_global=12)
    eng.save_report(report)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report_text(report))
        print(f"\nSaved: state/RISK_REPORT.md")
        print(f"Saved: state/risk_report.json")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
