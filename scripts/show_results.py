#!/usr/bin/env python3
"""
Show last simulation results in plain language.

Reads state/spatial_frame.json + pattern_memory + local graph and prints a
readable report. Also writes state/RESULTS.md (and optional HTML).

Usage:
  cd ~/projects/sovereign-demographic-engine
  source .venv/bin/activate
  python scripts/show_results.py
  python scripts/show_results.py --html   # also state/results.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
FRAME = STATE / "spatial_frame.json"
MEMORY = STATE / "pattern_memory.json"
GRAPH = STATE / "local_graph.json"
RESULTS_MD = STATE / "RESULTS.md"
RESULTS_HTML = STATE / "results.html"


def _load(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_report() -> dict:
    frame = _load(FRAME) or {}
    memory = _load(MEMORY) or {}
    graph = _load(GRAPH) or {}

    agents = frame.get("agents") or []
    metrics = frame.get("metrics") or {}
    origin = {}
    nodes = (graph or {}).get("nodes") or {}
    if isinstance(nodes, dict):
        origin = nodes.get("loc_mx_001") or {}

    rows = []
    total_remitted = 0.0
    for a in agents:
        rem = float(a.get("total_remitted") or 0)
        wealth = float(a.get("base_wealth") or 0)
        start = wealth + rem if rem else wealth
        # better start estimate if remitted known
        if rem > 0:
            start = wealth + rem
        yld = (rem / start * 100.0) if start > 0 else 0.0
        total_remitted += rem
        rows.append(
            {
                "id": a.get("id"),
                "name": a.get("name") or a.get("id"),
                "state": a.get("state"),
                "lat": a.get("lat"),
                "lon": a.get("lon"),
                "wealth": wealth,
                "remitted": rem,
                "rem_count": a.get("remittance_count", 0),
                "yield_pct": yld,
                "gravity_p": a.get("gravity_p"),
            }
        )

    pol = memory.get("policy") or {}
    w = memory.get("weights") or {}
    m = memory.get("metrics") or {}

    return {
        "tick": frame.get("tick"),
        "sim_time": frame.get("sim_time"),
        "backend": frame.get("backend"),
        "engine": frame.get("engine"),
        "metrics": metrics,
        "origin_wealth": metrics.get(
            "origin_aggregate_wealth", origin.get("aggregate_wealth", 0)
        ),
        "origin_events": origin.get("remittance_events", 0),
        "agents": rows,
        "total_remitted": total_remitted,
        "n_agents": len(rows),
        "settled": sum(1 for r in rows if r["state"] == "SETTLED"),
        "learning": {
            "episodes": memory.get("episode_count", 0),
            "buckets": len(memory.get("buckets") or {}),
            "remit_frac": pol.get("remittance_fraction"),
            "migrate_thr": pol.get("migrate_threshold"),
            "risk_bias": pol.get("risk_bias"),
            "alpha": w.get("alpha"),
            "beta": w.get("beta"),
            "gamma": w.get("gamma"),
            "avg_yield": m.get("avg_remit_yield"),
            "migrate_ok": m.get("avg_migrate_success"),
        },
        "updated_at": frame.get("updated_at"),
    }


def format_text(r: dict) -> str:
    lines = [
        "=" * 64,
        "  SOVEREIGN DEMOGRAPHIC ENGINE — RESULTS",
        "=" * 64,
        "",
        f"  Sim time:     {r.get('sim_time')}s   tick={r.get('tick')}   backend={r.get('backend')}",
        f"  Engine:       {r.get('engine')}",
        f"  Agents:       {r.get('settled')}/{r.get('n_agents')} SETTLED",
        f"  Origin wealth (Zacatecas remittances received):  ${float(r.get('origin_wealth') or 0):,.2f}",
        f"  Pulses:       spawned={r['metrics'].get('pulses_spawned')}  "
        f"settled={r['metrics'].get('pulses_settled')}  "
        f"active={r['metrics'].get('pulses_active')}",
        f"  Total remitted by agents:  ${float(r.get('total_remitted') or 0):,.2f}",
        "",
        "  AGENTS (destination = El Paso lat 31.76 lon -106.49 when SETTLED)",
        "  " + "-" * 60,
    ]
    for a in r.get("agents") or []:
        lines.append(
            f"  {a['name'] or a['id']}"
        )
        lines.append(
            f"    id={a['id']}  state={a['state']}  "
            f"lat={a.get('lat')} lon={a.get('lon')}"
        )
        lines.append(
            f"    wealth left ${a['wealth']:,.0f}  "
            f"sent home ${a['remitted']:,.0f}  "
            f"yield {a['yield_pct']:.1f}%  "
            f"pulses={a['rem_count']}  "
            f"gravity_p={a.get('gravity_p')}"
        )
        lines.append("")

    L = r.get("learning") or {}
    lines += [
        "  LEARNING (pattern memory)",
        "  " + "-" * 60,
        f"  episodes={L.get('episodes')}  buckets={L.get('buckets')}",
        f"  remit_fraction={L.get('remit_frac')}  "
        f"migrate_threshold={L.get('migrate_thr')}  "
        f"risk_bias={L.get('risk_bias')}",
        f"  gravity weights  α={L.get('alpha')}  β={L.get('beta')}  γ={L.get('gamma')}",
        f"  avg yield={L.get('avg_yield')}  migrate_ok={L.get('migrate_ok')}",
        "",
        "  FILES",
        "  " + "-" * 60,
        f"  {RESULTS_MD}",
        f"  {FRAME}",
        f"  {MEMORY}",
        f"  {ROOT / 'STATUS.md'}",
        "",
        "=" * 64,
        "  Story in one line:",
        f"  {r.get('n_agents')} people left origin → El Paso, sent "
        f"${float(r.get('total_remitted') or 0):,.0f} home; "
        f"origin now holds ${float(r.get('origin_wealth') or 0):,.0f}.",
        "=" * 64,
    ]
    return "\n".join(lines)


def format_md(r: dict) -> str:
    lines = [
        "# Sovereign Demographic Engine — Results",
        "",
        f"_Last frame: tick **{r.get('tick')}**, sim **{r.get('sim_time')}s**, "
        f"backend **{r.get('backend')}**_",
        "",
        "## Headline",
        "",
        f"- **{r.get('settled')}/{r.get('n_agents')}** agents **SETTLED** at El Paso",
        f"- **Origin (Zacatecas) remittance wealth:** "
        f"**${float(r.get('origin_wealth') or 0):,.2f}**",
        f"- **Capital pulses:** {r['metrics'].get('pulses_settled')} settled / "
        f"{r['metrics'].get('pulses_spawned')} spawned",
        f"- **Total remitted by agents:** **${float(r.get('total_remitted') or 0):,.2f}**",
        "",
        "## Agents",
        "",
        "| Name | State | Wealth left | Sent home | Yield | Pulses |",
        "|------|-------|------------:|----------:|------:|-------:|",
    ]
    for a in r.get("agents") or []:
        lines.append(
            f"| {a['name']} | {a['state']} | ${a['wealth']:,.0f} | "
            f"${a['remitted']:,.0f} | {a['yield_pct']:.1f}% | {a['rem_count']} |"
        )
    L = r.get("learning") or {}
    lines += [
        "",
        "## Pattern learning",
        "",
        f"| Field | Value |",
        f"|-------|------:|",
        f"| episodes | {L.get('episodes')} |",
        f"| remit_fraction | {L.get('remit_frac')} |",
        f"| risk_bias | {L.get('risk_bias')} |",
        f"| α (jobs) | {L.get('alpha')} |",
        f"| β (network) | {L.get('beta')} |",
        f"| γ (friction) | {L.get('gamma')} |",
        f"| migrate_ok | {L.get('migrate_ok')} |",
        "",
        "## Story",
        "",
        f"{r.get('n_agents')} people migrated Zacatecas → El Paso and sent "
        f"**${float(r.get('total_remitted') or 0):,.0f}** home as remittances. "
        f"Origin aggregate wealth is **${float(r.get('origin_wealth') or 0):,.0f}**.",
        "",
        "## How to re-run",
        "",
        "```bash",
        "cd ~/projects/sovereign-demographic-engine",
        "source .venv/bin/activate",
        "python layer4_spatial/spatial_engine.py --proto --no-api --quiet",
        "python scripts/show_results.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def format_html(r: dict) -> str:
    rows = ""
    for a in r.get("agents") or []:
        rows += (
            f"<tr><td>{a['name']}</td><td><b>{a['state']}</b></td>"
            f"<td>${a['wealth']:,.0f}</td><td>${a['remitted']:,.0f}</td>"
            f"<td>{a['yield_pct']:.1f}%</td><td>{a['rem_count']}</td></tr>\n"
        )
    L = r.get("learning") or {}
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Sovereign Demographic — Results</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto;
         padding: 0 1rem; background: #0f1419; color: #e7ecf1; }}
  h1 {{ font-size: 1.4rem; }}
  .card {{ background: #1a2332; border-radius: 12px; padding: 1.25rem; margin: 1rem 0; }}
  .big {{ font-size: 1.8rem; color: #3dd68c; font-weight: 700; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 0.5rem 0.4rem; border-bottom: 1px solid #2a3544; }}
  th {{ color: #8b9bb0; font-weight: 600; }}
  .muted {{ color: #8b9bb0; }}
</style>
</head>
<body>
  <h1>Sovereign Demographic Engine — Results</h1>
  <p class="muted">tick {r.get('tick')} · {r.get('sim_time')}s · {r.get('backend')}</p>
  <div class="card">
    <div class="muted">Origin remittance wealth (Zacatecas)</div>
    <div class="big">${float(r.get('origin_wealth') or 0):,.2f}</div>
    <p>{r.get('settled')}/{r.get('n_agents')} agents SETTLED ·
       pulses {r['metrics'].get('pulses_settled')}/{r['metrics'].get('pulses_spawned')} ·
       total remitted ${float(r.get('total_remitted') or 0):,.0f}</p>
  </div>
  <div class="card">
    <h2>Agents</h2>
    <table>
      <tr><th>Name</th><th>State</th><th>Wealth left</th><th>Sent home</th><th>Yield</th><th>Pulses</th></tr>
      {rows}
    </table>
  </div>
  <div class="card">
    <h2>Learning</h2>
    <p>episodes={L.get('episodes')} · remit_frac={L.get('remit_frac')} ·
       risk_bias={L.get('risk_bias')} · α={L.get('alpha')} β={L.get('beta')} γ={L.get('gamma')}</p>
  </div>
  <p class="muted">Open this file anytime: state/results.html · re-run with
  <code>python scripts/show_results.py --html</code></p>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Show last sim results")
    parser.add_argument("--html", action="store_true", help="Also write state/results.html")
    parser.add_argument("--quiet", action="store_true", help="Write files only, no print")
    args = parser.parse_args()

    if not FRAME.exists():
        print(
            "No results yet. Run the sim first:\n"
            "  cd ~/projects/sovereign-demographic-engine\n"
            "  source .venv/bin/activate\n"
            "  python layer4_spatial/spatial_engine.py --proto --no-api --quiet\n"
            "  python scripts/show_results.py",
            file=sys.stderr,
        )
        return 1

    report = build_report()
    text = format_text(report)
    md = format_md(report)
    STATE.mkdir(parents=True, exist_ok=True)
    RESULTS_MD.write_text(md, encoding="utf-8")
    results_txt = STATE / "RESULTS.txt"
    results_txt.write_text(text, encoding="utf-8")
    # Always write HTML so you can open it without flags
    RESULTS_HTML.write_text(format_html(report), encoding="utf-8")

    if not args.quiet:
        print(text)
        print(f"\n  Saved: {results_txt}")
        print(f"  Saved: {RESULTS_MD}")
        print(f"  Saved: {RESULTS_HTML}")
        print(f"  Open:  xdg-open {RESULTS_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
