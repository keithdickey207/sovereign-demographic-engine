#!/usr/bin/env python3
"""
Sovereign Debug Agent — keeps the demographic stack healthy and current.

Responsibilities:
  1. Audit graph / spatial / pattern state for drift and inconsistency
  2. Auto-fix safe issues (stale wealth, remittance edge bloat, missing files)
  3. Refresh status docs + state/debug_report.json so the prototype stays current
  4. Optional watch mode for continuous self-checks

Usage:
  python bridge/debug_agent.py              # audit only
  python bridge/debug_agent.py --fix       # audit + safe repairs
  python bridge/debug_agent.py --fix --sync-docs
  python bridge/debug_agent.py --watch 30   # re-audit every 30s
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.graph_store import GraphBackend, LOCAL_GRAPH_PATH, STATE_DIR  # noqa: E402
from bridge.pattern_memory import MEMORY_PATH, get_memory  # noqa: E402

FRAME_PATH = STATE_DIR / "spatial_frame.json"
DECISIONS_PATH = STATE_DIR / "tick_decisions.json"
RESOLVED_PATH = STATE_DIR / "resolved_persons.json"
REPORT_PATH = STATE_DIR / "debug_report.json"
STATUS_PATH = ROOT / "STATUS.md"

SEVERITY_ORDER = {"critical": 0, "error": 1, "warn": 2, "info": 3}


@dataclass
class Finding:
    code: str
    severity: str  # critical | error | warn | info
    message: str
    fixable: bool = False
    fixed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class DebugAgent:
    """Local self-healing auditor for the sovereign demographic stack."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.backend: GraphBackend | None = None

    def _add(
        self,
        code: str,
        severity: str,
        message: str,
        fixable: bool = False,
        **detail: Any,
    ) -> None:
        self.findings.append(
            Finding(
                code=code,
                severity=severity,
                message=message,
                fixable=fixable,
                detail=detail,
            )
        )

    # ── audits ───────────────────────────────────────────────────────

    def audit(self) -> list[Finding]:
        self.findings = []
        self.backend = GraphBackend()
        try:
            self._audit_filesystem()
            self._audit_graph()
            self._audit_resolved()
            self._audit_spatial_frame()
            self._audit_decisions()
            self._audit_pattern_memory()
            self._audit_code_surface()
        finally:
            if self.backend is not None:
                self.backend.close()
                self.backend = None
        return list(self.findings)

    def _audit_filesystem(self) -> None:
        required = [
            ROOT / "layer4_spatial" / "spatial_engine.py",
            ROOT / "layer3_inference" / "decision_daemon.py",
            ROOT / "layer2_ingest" / "resolve_entities.py",
            ROOT / "bridge" / "graph_store.py",
            ROOT / "bridge" / "pattern_memory.py",
            ROOT / "data" / "sample_ethnosurvey.csv",
            ROOT / "requirements.txt",
        ]
        for p in required:
            if not p.exists():
                self._add(
                    "missing_file",
                    "critical",
                    f"Required file missing: {p.relative_to(ROOT)}",
                    path=str(p),
                )
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not LOCAL_GRAPH_PATH.exists():
            self._add(
                "missing_graph",
                "error",
                "state/local_graph.json missing — will reseed",
                fixable=True,
            )

    def _audit_graph(self) -> None:
        assert self.backend is not None
        persons = self.backend.list_persons()
        if not persons:
            self._add(
                "no_persons",
                "error",
                "No Person nodes in graph",
                fixable=True,
            )
            return

        ids = [p["id"] for p in persons]
        if len(ids) != len(set(ids)):
            self._add("duplicate_person_ids", "error", "Duplicate person ids in graph")

        depleted = [
            p["id"]
            for p in persons
            if float(p.get("base_wealth") or 0) < 50 and not p.get("has_migrated")
        ]
        if depleted:
            self._add(
                "depleted_stayers",
                "warn",
                f"{len(depleted)} non-migrated agents with wealth < 50",
                fixable=True,
                agents=depleted,
            )

        all_migrated = all(bool(p.get("has_migrated")) for p in persons)
        if all_migrated:
            self._add(
                "all_migrated",
                "info",
                "All agents marked has_migrated — next force-migrate should auto-reset",
                n=len(persons),
            )

        origin = self.backend.get_location("loc_mx_001") or {}
        ow = float(origin.get("aggregate_wealth") or 0)
        ri = float(origin.get("remittance_inflow") or 0)
        events = int(origin.get("remittance_events") or 0)

        if abs(ow - ri) > 1.0 and ow > 0 and ri > 0:
            self._add(
                "wealth_inflow_mismatch",
                "warn",
                f"origin aggregate_wealth ({ow:.2f}) != remittance_inflow ({ri:.2f})",
                aggregate_wealth=ow,
                remittance_inflow=ri,
            )

        # Edge bloat on local backend
        if self.backend.backend == "local":
            snap = self.backend.local.snapshot()
            transfers = [
                e for e in snap.get("edges") or [] if e.get("type") == "TRANSFERRED_CAPITAL"
            ]
            if len(transfers) > 64:
                self._add(
                    "transfer_edge_bloat",
                    "warn",
                    f"{len(transfers)} TRANSFERRED_CAPITAL edges (demo noise)",
                    fixable=True,
                    count=len(transfers),
                )
            if events > 40 and ow > 15_000:
                self._add(
                    "origin_wealth_ratchet",
                    "warn",
                    f"Origin wealth {ow:.0f} after {events} events — likely multi-run ratchet",
                    fixable=True,
                    origin_wealth=ow,
                    events=events,
                )

        for p in persons:
            if "origin_location_id" not in p and self.backend.backend == "local":
                # soft: ensure ORIGINATES_FROM or default
                pass

    def _audit_resolved(self) -> None:
        if not RESOLVED_PATH.exists():
            self._add(
                "missing_resolved",
                "warn",
                "resolved_persons.json missing — run layer2 resolve",
                fixable=True,
            )
            return
        try:
            rows = json.loads(RESOLVED_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._add("resolved_corrupt", "error", f"resolved_persons unreadable: {exc}", fixable=True)
            return
        if not isinstance(rows, list) or not rows:
            self._add("resolved_empty", "error", "resolved_persons empty", fixable=True)
            return
        ids = [str(r.get("id")) for r in rows]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            self._add(
                "resolved_duplicate_ids",
                "error",
                f"Duplicate ids in resolved_persons: {dupes}",
                fixable=True,
                duplicates=dupes,
            )
        # Cross-check vs graph
        if self.backend:
            graph_ids = {p["id"] for p in self.backend.list_persons()}
            resolved_unique = set(ids)
            missing = resolved_unique - graph_ids
            # Allow subset; only flag if graph empty of resolved agents
            if graph_ids and not (graph_ids & resolved_unique):
                self._add(
                    "resolved_graph_disjoint",
                    "warn",
                    "resolved_persons ids do not overlap graph persons",
                    fixable=True,
                )

    def _audit_spatial_frame(self) -> None:
        if not FRAME_PATH.exists():
            self._add("no_spatial_frame", "info", "No spatial_frame.json yet (run engine once)")
            return
        try:
            frame = json.loads(FRAME_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._add("spatial_frame_corrupt", "error", f"spatial_frame unreadable: {exc}", fixable=True)
            return
        metrics = frame.get("metrics") or {}
        spawned = int(metrics.get("pulses_spawned") or 0)
        settled = int(metrics.get("pulses_settled") or 0)
        active = int(metrics.get("pulses_active") or 0)
        if spawned < settled:
            self._add(
                "pulse_accounting",
                "error",
                f"pulses_settled ({settled}) > pulses_spawned ({spawned})",
            )
        if active and frame.get("agents"):
            states = {a.get("state") for a in frame["agents"]}
            if states == {"SETTLED"} and active > 0:
                self._add(
                    "stale_active_pulses",
                    "warn",
                    "All agents SETTLED but pulses_active > 0 in last frame",
                    active=active,
                )
        # Engine identity
        if frame.get("engine") not in (None, "sovereign-spatial"):
            self._add(
                "unexpected_engine",
                "warn",
                f"Unexpected engine tag: {frame.get('engine')}",
            )

    def _audit_decisions(self) -> None:
        if not DECISIONS_PATH.exists():
            self._add("no_decisions", "info", "No tick_decisions.json (inference not run)")
            return
        try:
            data = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._add("decisions_corrupt", "error", f"tick_decisions unreadable: {exc}", fixable=True)
            return
        agents = data.get("agents") or {}
        for aid, d in agents.items():
            action = str((d or {}).get("action", "")).upper()
            if action not in ("MIGRATE", "STAY", "TRANSFER_CAPITAL", ""):
                self._add(
                    "bad_action",
                    "error",
                    f"Invalid action for {aid}: {action}",
                    agent=aid,
                    action=action,
                )

    def _audit_pattern_memory(self) -> None:
        mem = get_memory()
        snap = mem.snapshot()
        w = snap.get("weights") or {}
        for key in ("alpha", "beta", "gamma", "delta"):
            if key not in w:
                self._add("pattern_weights_incomplete", "warn", f"Missing weight {key}", fixable=True)
        pol = snap.get("policy") or {}
        rf = float(pol.get("remittance_fraction", 0.15))
        if not (0.05 <= rf <= 0.4):
            self._add(
                "policy_remit_oob",
                "error",
                f"remittance_fraction out of bounds: {rf}",
                fixable=True,
                remittance_fraction=rf,
            )
        if int(snap.get("episode_count") or 0) == 0:
            self._add(
                "pattern_cold",
                "info",
                "Pattern memory cold (0 episodes) — first runs will seed learning",
            )

    def _audit_code_surface(self) -> None:
        """Sanity: stack pointer docs mention custom spatial only."""
        for doc in (ROOT / "README.md", ROOT / "SEE_STACK.md"):
            if not doc.exists():
                continue
            text = doc.read_text(encoding="utf-8", errors="replace").lower()
            if "spatial" in text and "no spatial" not in text and "not use spatial" not in text:
                # README intentionally says no third-party game engines — only flag if it recommends the spatial engine
                if "requires spatial" in text or "install spatial" in text:
                    self._add(
                        "doc_third_party_engine_dep",
                        "warn",
                        f"{doc.name} may still recommend a third-party engine",
                        fixable=True,
                    )

    # ── fixes ────────────────────────────────────────────────────────

    def fix(self) -> list[Finding]:
        """Apply safe repairs for fixable findings. Re-audits after."""
        # Work from a fresh audit
        if not self.findings:
            self.audit()
        codes = {f.code for f in self.findings if f.fixable and not f.fixed}
        backend = GraphBackend()
        try:
            if "missing_graph" in codes or "no_persons" in codes:
                backend.apply_cypher_file(ROOT / "cypher" / "01_schema.cypher")
                self._mark_fixed("missing_graph")
                self._mark_fixed("no_persons")
                print("[debug] reseeded local graph schema")

            if "resolved_duplicate_ids" in codes or "missing_resolved" in codes or "resolved_empty" in codes or "resolved_corrupt" in codes or "resolved_graph_disjoint" in codes:
                self._rerun_resolve()
                self._mark_fixed("resolved_duplicate_ids")
                self._mark_fixed("missing_resolved")
                self._mark_fixed("resolved_empty")
                self._mark_fixed("resolved_corrupt")
                self._mark_fixed("resolved_graph_disjoint")

            if "transfer_edge_bloat" in codes or "origin_wealth_ratchet" in codes:
                self._purge_demo_economy(backend)
                self._mark_fixed("transfer_edge_bloat")
                self._mark_fixed("origin_wealth_ratchet")
                print("[debug] purged remittance edges + zeroed origin counters")

            if "depleted_stayers" in codes:
                self._restore_baseline_wealth(backend)
                self._mark_fixed("depleted_stayers")
                print("[debug] restored baseline wealth for depleted agents")

            if "spatial_frame_corrupt" in codes:
                if FRAME_PATH.exists():
                    FRAME_PATH.unlink()
                self._mark_fixed("spatial_frame_corrupt")

            if "decisions_corrupt" in codes:
                if DECISIONS_PATH.exists():
                    DECISIONS_PATH.unlink()
                self._mark_fixed("decisions_corrupt")

            if "pattern_weights_incomplete" in codes or "policy_remit_oob" in codes:
                # rewrite sane defaults while keeping episodes
                mem = get_memory()
                with mem._lock:
                    for k, v in {
                        "alpha": 1.0,
                        "beta": 1.2,
                        "gamma": 2.0,
                        "delta": 0.5,
                        "sigmoid_scale": 0.15,
                        "sigmoid_center": 20.0,
                    }.items():
                        mem.data["weights"].setdefault(k, v)
                    rf = float(mem.data["policy"].get("remittance_fraction", 0.15))
                    if not (0.05 <= rf <= 0.4):
                        mem.data["policy"]["remittance_fraction"] = 0.15
                mem.save()
                self._mark_fixed("pattern_weights_incomplete")
                self._mark_fixed("policy_remit_oob")
                print("[debug] repaired pattern memory weights/policy")
        finally:
            backend.close()

        # Re-audit for residual issues
        return self.audit()

    def _mark_fixed(self, code: str) -> None:
        for f in self.findings:
            if f.code == code and f.fixable:
                f.fixed = True

    def _rerun_resolve(self) -> None:
        import subprocess

        py = ROOT / ".venv" / "bin" / "python"
        cmd = [str(py if py.exists() else sys.executable), str(ROOT / "layer2_ingest" / "resolve_entities.py")]
        print(f"[debug] $ {' '.join(cmd)}")
        subprocess.call(cmd, cwd=str(ROOT))

    def _purge_demo_economy(self, backend: GraphBackend) -> None:
        """Zero origin counters, drop TRANSFERRED_CAPITAL edges (local), reset migrate flags optionally."""
        if backend.backend == "local":
            with backend.local._lock:
                data = backend.local._read()
                # Keep one seed transfer if present with year 2024, else drop all
                kept = []
                for e in data.get("edges") or []:
                    if e.get("type") != "TRANSFERRED_CAPITAL":
                        kept.append(e)
                    else:
                        props = e.get("props") or {}
                        if props.get("year") == 2024 and props.get("amount") == 25000:
                            kept.append(e)
                data["edges"] = kept
                for loc_id in ("loc_mx_001", "loc_us_001"):
                    node = data["nodes"].get(loc_id)
                    if node:
                        node["aggregate_wealth"] = 0.0
                        node["remittance_inflow"] = 0.0
                        node["remittance_events"] = 0
                backend.local._write(data)
        else:
            # Neo4j: zero counters only (safer than mass-delete edges)
            if backend.driver:
                with backend.driver.session() as session:
                    session.run(
                        """
                        MATCH (o:Location)
                        SET o.aggregate_wealth = 0,
                            o.remittance_inflow = 0,
                            o.remittance_events = 0
                        """
                    )

    def _restore_baseline_wealth(self, backend: GraphBackend) -> None:
        baselines: dict[str, float] = {}
        if RESOLVED_PATH.exists():
            try:
                for row in json.loads(RESOLVED_PATH.read_text(encoding="utf-8")):
                    baselines[str(row["id"])] = float(row.get("base_wealth") or 1500)
            except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
                pass
        for p in backend.list_persons():
            pid = p["id"]
            wealth = float(p.get("base_wealth") or 0)
            if wealth < 50 and not p.get("has_migrated"):
                backend.update_person(
                    pid,
                    {
                        "base_wealth": baselines.get(pid, 1500.0),
                        "total_remitted": 0,
                    },
                )

    # ── reporting / docs ─────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        counts = {"critical": 0, "error": 0, "warn": 0, "info": 0}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        healthy = counts["critical"] == 0 and counts["error"] == 0
        payload = {
            "agent": "sovereign-debug",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "healthy": healthy,
            "counts": counts,
            "findings": [asdict(f) for f in sorted(self.findings, key=lambda x: SEVERITY_ORDER.get(x.severity, 9))],
            "pattern": get_memory().summary_lines(),
            "stack": {
                "root": str(ROOT),
                "graph": str(LOCAL_GRAPH_PATH),
                "memory": str(MEMORY_PATH),
            },
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def sync_docs(self, report: dict[str, Any]) -> None:
        """Rewrite STATUS.md so humans/agents see live health."""
        lines = [
            "# Sovereign Demographic Engine — Live Status",
            "",
            f"_Updated by debug agent at `{report['ts']}`_",
            "",
            f"**Health:** `{'OK' if report['healthy'] else 'NEEDS ATTENTION'}`",
            "",
            "## Finding counts",
            "",
            "| Severity | Count |",
            "|----------|------:|",
        ]
        for sev in ("critical", "error", "warn", "info"):
            lines.append(f"| {sev} | {report['counts'].get(sev, 0)} |")
        lines += ["", "## Findings", ""]
        if not report["findings"]:
            lines.append("_No findings._")
        else:
            for f in report["findings"]:
                flag = " (fixed)" if f.get("fixed") else (" [fixable]" if f.get("fixable") else "")
                lines.append(f"- **{f['severity']}** `{f['code']}`{flag}: {f['message']}")
        lines += ["", "## Pattern memory", ""]
        for s in report.get("pattern") or []:
            lines.append(f"- `{s}`")
        lines += [
            "",
            "## Quick commands",
            "",
            "```bash",
            "# Full offline prototype (clean economy + learn + settle)",
            "python scripts/prototype.py",
            "",
            "# Debug agent",
            "python bridge/debug_agent.py --fix --sync-docs",
            "",
            "# Spatial corridor burn-in",
            "python layer4_spatial/spatial_engine.py --proto --no-api",
            "```",
            "",
            "## Stack rules",
            "",
            "- Custom spatial only (`sovereign-spatial`) — no third-party game engines/Unity/Unreal",
            "- Pattern memory in `state/pattern_memory.json` evolves across runs",
            "- Debug report: `state/debug_report.json`",
            "",
        ]
        STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"[debug] wrote {STATUS_PATH.relative_to(ROOT)}")

    def print_human(self, report: dict[str, Any]) -> None:
        status = "OK" if report["healthy"] else "NEEDS ATTENTION"
        print(f"[debug] health={status}  findings={report['counts']}")
        for f in report["findings"]:
            tag = "FIXED" if f.get("fixed") else f["severity"].upper()
            print(f"  [{tag}] {f['code']}: {f['message']}")
        print("[debug] pattern memory:")
        for s in report.get("pattern") or []:
            print(f"  {s}")
        print(f"[debug] report → {REPORT_PATH.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sovereign debug agent")
    parser.add_argument("--fix", action="store_true", help="Apply safe auto-repairs")
    parser.add_argument("--sync-docs", action="store_true", help="Refresh STATUS.md")
    parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help="Re-run every N seconds (0 = once)",
    )
    args = parser.parse_args()

    def once() -> int:
        agent = DebugAgent()
        if args.fix:
            agent.fix()
        else:
            agent.audit()
        report = agent.report()
        if args.sync_docs:
            agent.sync_docs(report)
        agent.print_human(report)
        return 0 if report["healthy"] else 1

    if args.watch > 0:
        print(f"[debug] watch every {args.watch}s (Ctrl+C to stop)")
        try:
            while True:
                rc = once()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n[debug] watch stopped")
            return 0
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
