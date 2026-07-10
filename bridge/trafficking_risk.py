#!/usr/bin/env python3
"""
Global trafficking RISK pattern engine (prevention / research).

Purpose
-------
Given an area (name, country, or lat/lon), estimate *structural* risk that
conditions associated with human trafficking — including labor and sex
trafficking — may be elevated. Designed for:
  - researchers, NGOs, policy analysts, law-enforcement *prevention* planning
  - global-scale pattern comparison across corridors

This is NOT
-----------
  - proof that trafficking is occurring
  - a list of victims or perpetrators
  - guidance on how to traffic people
  - a substitute for law enforcement investigation

Indicators are public-research style composites (poverty, conflict, demand
proxies, recruitment density, remittance dependency, etc.). Scores are
heuristic and must be validated with local data.

Minors: child_exploitation_risk is flagged as a protection priority only;
no sexual content about minors is generated.
"""

from __future__ import annotations

import json
import math
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = ROOT / "state"
NODES_PATH = DATA / "global_nodes.json"
CORRIDORS_PATH = DATA / "global_corridors.json"
REPORT_PATH = STATE / "risk_report.json"
REPORT_MD = STATE / "RISK_REPORT.md"

# Weights for composite structural risk (sum ≈ 1.0)
WEIGHTS = {
    "push_poverty": 0.14,
    "push_unemployment": 0.10,
    "push_female_gap": 0.08,
    "governance_weak": 0.12,  # inverse rule_of_law + corruption + conflict
    "border_opacity": 0.10,
    "recruitment": 0.12,
    "remittance_pressure": 0.08,
    "digital_recruitment": 0.08,
    "demand_sex": 0.09,
    "demand_labor": 0.09,
}

# Protective factor: child protection capacity lowers child risk
CHILD_PROTECT_WEIGHT = 0.35


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class TraffickingRiskEngine:
    """Global-scale structural risk scanner."""

    def __init__(
        self,
        nodes_path: Path = NODES_PATH,
        corridors_path: Path = CORRIDORS_PATH,
    ) -> None:
        self.nodes_path = nodes_path
        self.corridors_path = corridors_path
        self.nodes: list[dict[str, Any]] = []
        self.corridors: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        nodes_doc = json.loads(self.nodes_path.read_text(encoding="utf-8"))
        corr_doc = json.loads(self.corridors_path.read_text(encoding="utf-8"))
        self.nodes = list(nodes_doc.get("nodes") or [])
        self.corridors = list(corr_doc.get("corridors") or [])
        self._by_id = {n["id"]: n for n in self.nodes}

    def list_areas(self) -> list[dict[str, Any]]:
        return [
            {
                "id": n["id"],
                "name": n["name"],
                "country": n["country"],
                "region": n["region"],
                "lat": n["lat"],
                "lon": n["lon"],
                "role": n.get("role"),
            }
            for n in self.nodes
        ]

    def resolve_area(
        self,
        query: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float = 800.0,
    ) -> list[dict[str, Any]]:
        """
        Resolve user area input to one or more nodes.
        - text query: fuzzy match name/country/region/id
        - lat/lon: nearest nodes within radius
        """
        hits: list[dict[str, Any]] = []

        if lat is not None and lon is not None:
            ranked = []
            for n in self.nodes:
                d = _haversine_km(lat, lon, float(n["lat"]), float(n["lon"]))
                ranked.append((d, n))
            ranked.sort(key=lambda x: x[0])
            for d, n in ranked:
                if d <= radius_km or len(hits) < 3:
                    item = deepcopy(n)
                    item["_distance_km"] = round(d, 1)
                    hits.append(item)
                if len(hits) >= 5:
                    break
            return hits

        if not query:
            return []

        q = query.strip().lower()
        q_compact = re.sub(r"[^a-z0-9]+", "", q)

        for n in self.nodes:
            fields = [
                str(n.get("id", "")),
                str(n.get("name", "")),
                str(n.get("country", "")),
                str(n.get("region", "")),
            ]
            blob = " ".join(fields).lower()
            compact = re.sub(r"[^a-z0-9]+", "", blob)
            score = 0.0
            if q in blob or q_compact in compact:
                score = 1.0
            else:
                # token overlap
                tokens = [t for t in re.split(r"\W+", q) if t]
                if tokens:
                    score = sum(1 for t in tokens if t in blob) / len(tokens)
            if score >= 0.5:
                item = deepcopy(n)
                item["_match_score"] = round(score, 3)
                hits.append(item)

        hits.sort(key=lambda x: x.get("_match_score", 0), reverse=True)
        return hits

    def score_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """Compute structural risk components for one location."""
        poverty = float(node.get("poverty_index", 0.5))
        unemp = float(node.get("youth_unemployment", 0.5))
        fgap = float(node.get("female_labor_gap", 0.5))
        rol = float(node.get("rule_of_law", 0.5))
        conflict = float(node.get("conflict_index", 0.3))
        corr = float(node.get("corruption_index", 0.5))
        border = float(node.get("border_opacity", 0.4))
        recruit = float(node.get("recruitment_network_density", 0.5))
        remit = float(node.get("remittance_dependency", 0.4))
        digital = float(node.get("digital_recruitment_exposure", 0.5))
        sex_d = float(node.get("sex_industry_demand_proxy", 0.4))
        labor_d = float(node.get("labor_demand_proxy", 0.5))
        child_cap = float(node.get("child_protection_capacity", 0.5))

        governance_weak = _clamp(0.4 * (1.0 - rol) + 0.35 * corr + 0.25 * conflict)

        components = {
            "push_poverty": poverty,
            "push_unemployment": unemp,
            "push_female_gap": fgap,
            "governance_weak": governance_weak,
            "border_opacity": border,
            "recruitment": recruit,
            "remittance_pressure": remit,
            "digital_recruitment": digital,
            "demand_sex": sex_d,
            "demand_labor": labor_d,
        }

        overall = sum(WEIGHTS[k] * components[k] for k in WEIGHTS)
        overall = _clamp(overall)

        # Typology-specific scores
        labor_risk = _clamp(
            0.25 * poverty
            + 0.20 * unemp
            + 0.20 * labor_d
            + 0.15 * recruit
            + 0.10 * remit
            + 0.10 * governance_weak
        )
        sex_risk = _clamp(
            0.22 * sex_d
            + 0.18 * fgap
            + 0.15 * poverty
            + 0.15 * recruit
            + 0.12 * digital
            + 0.10 * governance_weak
            + 0.08 * border
        )
        # Child exploitation RISK (protection lens) — elevated by weak protection + push
        child_risk = _clamp(
            0.30 * (1.0 - child_cap)
            + 0.25 * poverty
            + 0.20 * conflict
            + 0.15 * recruit
            + 0.10 * border
        )

        role = node.get("role") or []
        role_boost = 0.0
        if "transit" in role or "border_hub" in role or "landing" in role:
            role_boost += 0.04
        if "detention_risk" in role or "recruitment_hub" in role:
            role_boost += 0.06
        overall = _clamp(overall + role_boost)
        labor_risk = _clamp(labor_risk + role_boost * 0.5)
        sex_risk = _clamp(sex_risk + role_boost * 0.5)

        # Prioritize places with documented high involvement (public reporting / TIP-style)
        known = str(node.get("known_involvement") or "watch").lower()
        inv_types = [str(t).lower() for t in (node.get("involvement_types") or [])]
        known_boost = {
            "confirmed_high": 0.12,
            "elevated": 0.06,
            "moderate": 0.02,
            "watch": 0.0,
        }.get(known, 0.0)
        overall = _clamp(overall + known_boost)
        if "sex_trafficking" in inv_types:
            sex_risk = _clamp(sex_risk + known_boost * 0.9)
        if "labor_trafficking" in inv_types or "forced_scam_compound" in inv_types:
            labor_risk = _clamp(labor_risk + known_boost * 0.9)
        if "child_exploitation" in inv_types:
            child_risk = _clamp(child_risk + known_boost * 0.8)

        return {
            "overall_0_100": round(overall * 100, 1),
            "labor_trafficking_risk_0_100": round(labor_risk * 100, 1),
            "sex_trafficking_risk_0_100": round(sex_risk * 100, 1),
            "child_exploitation_risk_0_100": round(child_risk * 100, 1),
            "level": self._level(overall),
            "known_involvement": known,
            "involvement_types": inv_types,
            "source_tags": list(node.get("source_tags") or []),
            "components": {k: round(v, 3) for k, v in components.items()},
            "protective": {
                "rule_of_law": rol,
                "child_protection_capacity": child_cap,
            },
        }

    def rank_hotspots(
        self,
        kind: str = "all",
        min_level: str = "elevated",
        limit: int = 25,
        known_only: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Rank global areas by risk, optionally only known high-involvement nodes.

        kind: all | sex | labor | child
        known_only: if True, only confirmed_high / elevated tagged areas
        """
        level_floor = {
            "critical": 0.75,
            "high": 0.60,
            "elevated": 0.45,
            "moderate": 0.30,
            "lower": 0.0,
        }.get(min_level.lower(), 0.45)

        kind = kind.lower()
        rows: list[dict[str, Any]] = []
        for n in self.nodes:
            known = str(n.get("known_involvement") or "watch").lower()
            if known_only and known not in ("confirmed_high", "elevated"):
                continue
            sc = self.score_node(n)
            if kind == "sex":
                metric = sc["sex_trafficking_risk_0_100"]
            elif kind == "labor":
                metric = sc["labor_trafficking_risk_0_100"]
            elif kind == "child":
                metric = sc["child_exploitation_risk_0_100"]
            else:
                metric = sc["overall_0_100"]
            if metric / 100.0 < level_floor and known != "confirmed_high":
                continue
            corridors = self.corridors_for_node(n["id"])
            rows.append(
                {
                    "rank": 0,
                    "id": n["id"],
                    "name": n["name"],
                    "country": n["country"],
                    "region": n["region"],
                    "lat": n["lat"],
                    "lon": n["lon"],
                    "known_involvement": known,
                    "involvement_types": n.get("involvement_types") or [],
                    "source_tags": n.get("source_tags") or [],
                    "metric": metric,
                    "overall_0_100": sc["overall_0_100"],
                    "sex_trafficking_risk_0_100": sc["sex_trafficking_risk_0_100"],
                    "labor_trafficking_risk_0_100": sc["labor_trafficking_risk_0_100"],
                    "child_exploitation_risk_0_100": sc["child_exploitation_risk_0_100"],
                    "level": sc["level"],
                    "corridor_count": len(corridors),
                    "top_corridor": (corridors[0]["name"] if corridors else None),
                    "notes": n.get("notes"),
                }
            )
        rows.sort(
            key=lambda r: (
                0 if r["known_involvement"] == "confirmed_high" else 1,
                -float(r["metric"]),
            )
        )
        for i, r in enumerate(rows[:limit], 1):
            r["rank"] = i
        return rows[:limit]

    @staticmethod
    def _level(score_0_1: float) -> str:
        if score_0_1 >= 0.75:
            return "CRITICAL"
        if score_0_1 >= 0.60:
            return "HIGH"
        if score_0_1 >= 0.45:
            return "ELEVATED"
        if score_0_1 >= 0.30:
            return "MODERATE"
        return "LOWER"

    def corridors_for_node(self, node_id: str) -> list[dict[str, Any]]:
        out = []
        for c in self.corridors:
            involved = {c.get("origin"), c.get("destination"), *(c.get("via") or [])}
            if node_id in involved:
                item = deepcopy(c)
                item["origin_name"] = (self._by_id.get(c["origin"]) or {}).get("name", c["origin"])
                item["destination_name"] = (self._by_id.get(c["destination"]) or {}).get(
                    "name", c["destination"]
                )
                out.append(item)
        out.sort(key=lambda x: float(x.get("intensity", 0)), reverse=True)
        return out

    def pattern_narrative(self, node: dict[str, Any], scores: dict[str, Any]) -> list[str]:
        """Human-readable potential patterns (risk language)."""
        c = scores["components"]
        patterns: list[str] = []

        if c["push_poverty"] >= 0.55 and c["demand_labor"] >= 0.5:
            patterns.append(
                "Economic push–pull: high poverty with external labor demand can "
                "create debt-financed migration that networks may exploit (labor trafficking risk)."
            )
        if c["push_female_gap"] >= 0.45 and c["demand_sex"] >= 0.45:
            patterns.append(
                "Gendered vulnerability + commercial-sex demand proxy: elevated structural "
                "conditions associated with sex trafficking risk (not proof of cases)."
            )
        if c["recruitment"] >= 0.55 and c["remittance_pressure"] >= 0.55:
            patterns.append(
                "Recruitment density under remittance pressure: informal brokers and "
                "placement fees are classic debt-bondage entry patterns in research literature."
            )
        if c["governance_weak"] >= 0.55 and c["border_opacity"] >= 0.5:
            patterns.append(
                "Weak governance + opaque borders: transit zones where coercion and "
                "extortion risks rise for people on the move."
            )
        if c["digital_recruitment"] >= 0.55 and scores["sex_trafficking_risk_0_100"] >= 50:
            patterns.append(
                "Digital recruitment exposure: online job/romance lures are a documented "
                "pathway into exploitation; monitor fake overseas offers."
            )
        if scores["child_exploitation_risk_0_100"] >= 55:
            patterns.append(
                "CHILD PROTECTION PRIORITY: weak protection capacity combined with poverty/"
                "conflict elevates child exploitation risk — route to child-protection channels."
            )
        roles = node.get("role") or []
        if "transit" in roles or "landing" in roles:
            patterns.append(
                "Transit/landing role: people are often most vulnerable at intermediate "
                "stops (documents held, isolation, unpaid 'debts')."
            )
        if "destination" in roles and c["demand_sex"] >= 0.5:
            patterns.append(
                "Destination demand hub: higher commercial-sex market demand proxies "
                "correlate with sex trafficking victimization risk in destination cities."
            )
        if not patterns:
            patterns.append(
                "No single extreme pattern dominates; residual migration exploitation risk "
                "still exists wherever irregular movement and information asymmetry meet."
            )
        return patterns

    def prevention_actions(self, scores: dict[str, Any]) -> list[str]:
        actions = [
            "Treat this as a RISK screen for prevention planning — not an accusation.",
            "Cross-check with official TIP reports, UNODC, IOM, and local NGO caseloads.",
            "If imminent harm is suspected: contact local emergency services / national hotlines.",
        ]
        if scores["labor_trafficking_risk_0_100"] >= 55:
            actions.append(
                "Labor: expand ethical recruitment oversight, wage recovery, and worksite inspection."
            )
        if scores["sex_trafficking_risk_0_100"] >= 55:
            actions.append(
                "Sex trafficking risk: fund exit services, victim-centered policing, and "
                "demand-reduction that does not criminalize victims."
            )
        if scores["child_exploitation_risk_0_100"] >= 50:
            actions.append(
                "Children: strengthen guardianship, school retention, and specialized "
                "child-protection referrals (never publish identifying data on minors)."
            )
        if scores["components"]["digital_recruitment"] >= 0.55:
            actions.append(
                "Digital: public-awareness on fake job/romance offers; platform reporting paths."
            )
        return actions

    def scan_area(
        self,
        query: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        top_global: int = 8,
    ) -> dict[str, Any]:
        """Full scan: resolve area, score, attach corridors, global comparables."""
        matches = self.resolve_area(query=query, lat=lat, lon=lon)
        if not matches:
            # Global fallback: return top risk nodes worldwide
            ranked = []
            for n in self.nodes:
                s = self.score_node(n)
                ranked.append((s["overall_0_100"], n, s))
            ranked.sort(key=lambda x: x[0], reverse=True)
            return {
                "ok": False,
                "error": f"No node matched query={query!r} lat={lat} lon={lon}",
                "hint": "Try: El Paso, Lagos, Bangkok, Dubai, Berlin, Mumbai, Sicily, Zacatecas",
                "global_hotspots": [
                    {
                        "name": n["name"],
                        "country": n["country"],
                        "score": sc["overall_0_100"],
                        "level": sc["level"],
                    }
                    for _, n, sc in ranked[:top_global]
                ],
                "disclaimer": self.disclaimer(),
            }

        primary = matches[0]
        score = self.score_node(primary)
        corridors = self.corridors_for_node(primary["id"])

        # Global comparables (similar score band or same region)
        comparables = []
        for n in self.nodes:
            if n["id"] == primary["id"]:
                continue
            sc = self.score_node(n)
            similarity = 1.0 - abs(sc["overall_0_100"] - score["overall_0_100"]) / 100.0
            region_bonus = 0.15 if n.get("region") == primary.get("region") else 0.0
            comparables.append(
                {
                    "name": n["name"],
                    "country": n["country"],
                    "region": n["region"],
                    "overall_0_100": sc["overall_0_100"],
                    "sex_trafficking_risk_0_100": sc["sex_trafficking_risk_0_100"],
                    "labor_trafficking_risk_0_100": sc["labor_trafficking_risk_0_100"],
                    "level": sc["level"],
                    "similarity": round(similarity + region_bonus, 3),
                }
            )
        comparables.sort(key=lambda x: x["similarity"], reverse=True)

        # World rank
        world = []
        for n in self.nodes:
            sc = self.score_node(n)
            world.append((sc["overall_0_100"], n["name"], n["country"], sc["level"]))
        world.sort(reverse=True)
        rank = next(
            (i + 1 for i, w in enumerate(world) if w[1] == primary["name"]),
            None,
        )

        # Always attach ranked known high-involvement set for global context
        hotspots = self.rank_hotspots(kind="all", known_only=True, limit=top_global)

        report = {
            "ok": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "query": {"text": query, "lat": lat, "lon": lon},
            "area": {
                "id": primary["id"],
                "name": primary["name"],
                "country": primary["country"],
                "region": primary["region"],
                "lat": primary["lat"],
                "lon": primary["lon"],
                "role": primary.get("role"),
                "notes": primary.get("notes"),
                "known_involvement": primary.get("known_involvement"),
                "involvement_types": primary.get("involvement_types") or [],
                "source_tags": primary.get("source_tags") or [],
                "distance_km": primary.get("_distance_km"),
                "match_score": primary.get("_match_score"),
            },
            "scores": score,
            "patterns": self.pattern_narrative(primary, score),
            "prevention": self.prevention_actions(score),
            "corridors": corridors,
            "known_high_involvement_global": hotspots,
            "alternate_matches": [
                {"name": m["name"], "country": m["country"], "id": m["id"]}
                for m in matches[1:5]
            ],
            "global_comparables": comparables[:top_global],
            "world_rank": {"rank": rank, "of": len(world)},
            "world_top": [
                {"rank": i + 1, "name": w[1], "country": w[2], "score": w[0], "level": w[3]}
                for i, w in enumerate(world[:top_global])
            ],
            "coverage": {
                "nodes": len(self.nodes),
                "corridors": len(self.corridors),
                "scale": "global_seed",
            },
            "disclaimer": self.disclaimer(),
        }
        return report

    @staticmethod
    def disclaimer() -> str:
        return (
            "STRUCTURAL RISK MODEL for prevention and research only. "
            "Scores are heuristic composites from seed indicators — not legal findings, "
            "not confirmation of trafficking, and not a targeting tool against individuals. "
            "Do not use to harass migrants or sex workers. "
            "Validate with official sources (national TIP reports, UNODC, IOM, police). "
            "For emergencies use local law enforcement / trafficking hotlines."
        )

    def save_report(self, report: dict[str, Any]) -> tuple[Path, Path]:
        STATE.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        REPORT_MD.write_text(format_report_md(report), encoding="utf-8")
        return REPORT_PATH, REPORT_MD


def format_report_md(r: dict[str, Any]) -> str:
    lines = [
        "# Global Trafficking RISK Scan",
        "",
        f"_Generated: `{r.get('generated_at')}`_",
        "",
        "> " + (r.get("disclaimer") or TraffickingRiskEngine.disclaimer()),
        "",
    ]
    if r.get("mode") == "hotspots":
        lines += [
            "## Known high-involvement hotspots",
            "",
            f"Kind: `{r.get('kind')}` · count: {r.get('count')}",
            "",
            "| Rank | Area | Country | Overall | Sex | Labor | Tag |",
            "|-----:|------|---------|--------:|----:|------:|-----|",
        ]
        for h in r.get("hotspots") or []:
            lines.append(
                f"| {h.get('rank')} | {h.get('name')} | {h.get('country')} | "
                f"{h.get('overall_0_100')} | {h.get('sex_trafficking_risk_0_100')} | "
                f"{h.get('labor_trafficking_risk_0_100')} | {h.get('known_involvement')} |"
            )
        return "\n".join(lines) + "\n"

    if not r.get("ok"):
        lines += [
            "## No match",
            "",
            r.get("error", ""),
            "",
            r.get("hint", ""),
            "",
            "## Global hotspots (seed data)",
            "",
        ]
        for h in r.get("global_hotspots") or []:
            lines.append(
                f"- **{h['name']}** ({h['country']}): {h['score']} — {h['level']}"
            )
        return "\n".join(lines) + "\n"

    a = r["area"]
    s = r["scores"]
    lines += [
        "## Area",
        "",
        f"- **{a['name']}**, {a['country']} ({a['region']})",
        f"- Coordinates: `{a['lat']}, {a['lon']}`",
        f"- Roles: {', '.join(a.get('role') or [])}",
        f"- **Known involvement tag:** `{a.get('known_involvement')}` "
        f"types={', '.join(a.get('involvement_types') or []) or 'n/a'}",
        f"- Sources: {', '.join(a.get('source_tags') or []) or 'seed model'}",
        f"- Notes: {a.get('notes')}",
        "",
        "## Risk scores (0–100 structural)",
        "",
        f"| Metric | Score | Level |",
        f"|--------|------:|-------|",
        f"| **Overall exploitation risk** | **{s['overall_0_100']}** | **{s['level']}** |",
        f"| Labor trafficking risk | {s['labor_trafficking_risk_0_100']} | |",
        f"| Sex trafficking risk | {s['sex_trafficking_risk_0_100']} | |",
        f"| Child exploitation risk (protection) | {s['child_exploitation_risk_0_100']} | |",
        "",
        f"World rank in seed set: **#{r['world_rank']['rank']}** of {r['world_rank']['of']}",
        "",
        "## Potential patterns",
        "",
    ]
    for p in r.get("patterns") or []:
        lines.append(f"- {p}")
    lines += ["", "## Linked global corridors", ""]
    if not r.get("corridors"):
        lines.append("_No corridor templates link this node yet._")
    for c in r.get("corridors") or []:
        lines.append(
            f"- **{c['name']}** (intensity {c.get('intensity')})  \n"
            f"  {c.get('origin_name')} → {c.get('destination_name')}  \n"
            f"  Typologies: {', '.join(c.get('typologies') or [])}  \n"
            f"  Signals: {', '.join(c.get('signals') or [])}"
        )
    lines += ["", "## Prevention / response notes", ""]
    for p in r.get("prevention") or []:
        lines.append(f"- {p}")
    lines += ["", "## Similar areas worldwide", ""]
    for g in r.get("global_comparables") or []:
        lines.append(
            f"- {g['name']} ({g['country']}) overall={g['overall_0_100']} "
            f"sex_risk={g['sex_trafficking_risk_0_100']} "
            f"labor_risk={g['labor_trafficking_risk_0_100']} [{g['level']}]"
        )
    lines += ["", "## Known high-involvement hotspots (global priority list)", ""]
    for h in r.get("known_high_involvement_global") or []:
        lines.append(
            f"{h.get('rank')}. **{h['name']}** ({h['country']}) — "
            f"overall={h['overall_0_100']} sex={h['sex_trafficking_risk_0_100']} "
            f"labor={h['labor_trafficking_risk_0_100']} "
            f"[{h['known_involvement']}] {h['level']}"
        )
    lines += ["", "## World top (seed coverage)", ""]
    for w in r.get("world_top") or []:
        lines.append(
            f"{w['rank']}. {w['name']} ({w['country']}) — {w['score']} {w['level']}"
        )
    lines += [
        "",
        "## Coverage",
        "",
        f"Seed nodes: {r.get('coverage', {}).get('nodes')} · "
        f"corridors: {r.get('coverage', {}).get('corridors')} · "
        f"scale: {r.get('coverage', {}).get('scale')}",
        "",
        "Extend `data/global_nodes.json` and `data/global_corridors.json` for denser world coverage.",
        "",
    ]
    return "\n".join(lines)


def format_report_text(r: dict[str, Any]) -> str:
    return format_report_md(r).replace("**", "").replace("# ", "").replace("## ", "")
