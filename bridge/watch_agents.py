#!/usr/bin/env python3
"""
Watch-agent training & detection (protection-oriented).

Trains specialized agents to recognize:
  - vulnerable person profiles
  - risk groups / clusters
  - recruitment & demand network patterns

Use for prevention, triage, research. NOT for stalking or harming people.
"""

from __future__ import annotations

import json
import math
import random
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = ROOT / "state"
PROFILES_PATH = DATA / "watch_profiles.json"
MODEL_PATH = STATE / "watch_agents.json"
HITS_PATH = STATE / "watch_hits.json"
REPORT_MD = STATE / "WATCH_REPORT.md"

try:
    from bridge.trafficking_risk import TraffickingRiskEngine
except Exception:  # pragma: no cover
    TraffickingRiskEngine = None  # type: ignore


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _age_from_dob(dob: str | None) -> int | None:
    if not dob:
        return None
    try:
        year = int(str(dob)[:4])
        # sim clock year ~2026
        return max(0, 2026 - year)
    except (TypeError, ValueError):
        return None


class WatchAgentTrainer:
    """Train and run watch agents against people / groups / places."""

    def __init__(self, profiles_path: Path = PROFILES_PATH) -> None:
        self.profiles_path = profiles_path
        doc = json.loads(profiles_path.read_text(encoding="utf-8"))
        self.profiles: list[dict[str, Any]] = list(doc.get("profiles") or [])
        self.model: dict[str, Any] = self._default_model()
        if MODEL_PATH.exists():
            try:
                self.model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        self.risk = TraffickingRiskEngine() if TraffickingRiskEngine else None

    def _default_model(self) -> dict[str, Any]:
        agents = {}
        for p in self.profiles:
            agents[p["id"]] = {
                "profile_id": p["id"],
                "label": p["label"],
                "category": p["category"],
                "priority": p["priority"],
                "weights": dict(p.get("weights") or {}),
                "threshold": 0.55 if p["priority"] != "critical" else 0.40,
                "trained_epochs": 0,
                "samples_seen": 0,
                "true_pos_ema": 0.0,
                "false_pos_ema": 0.0,
                "accuracy_ema": 0.5,
            }
        return {
            "version": 1,
            "updated_at": None,
            "agents": agents,
            "training_log": [],
        }

    # ── feature builders ─────────────────────────────────────────────

    def person_features(self, person: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, float]:
        ctx = context or {}
        wealth = float(person.get("base_wealth") or 1500)
        net = float(person.get("network_size") or 0)
        origin_jobs = float(person.get("origin_jobs") or 10)
        dest_jobs = float(person.get("dest_jobs") or 50)
        border = float(person.get("border_friction") or ctx.get("border_opacity") or 0.5)
        risk_t = float(person.get("risk_tolerance") or 0.5)
        pull = dest_jobs / max(origin_jobs, 1.0)
        age = person.get("age")
        if age is None:
            age = _age_from_dob(person.get("dob"))
        age = float(age) if age is not None else 30.0

        remit = float(ctx.get("remittance_dependency") or person.get("remittance_dependency") or 0.4)
        conflict = float(ctx.get("conflict_index") or 0.2)
        gov_weak = float(ctx.get("governance_weak") or (1.0 - float(ctx.get("rule_of_law") or 0.5)))
        digital = float(ctx.get("digital_recruitment_exposure") or 0.4)
        recruit = float(ctx.get("recruitment_network_density") or 0.4)
        sex_d = float(ctx.get("sex_industry_demand_proxy") or 0.3)
        labor_d = float(ctx.get("labor_demand_proxy") or 0.4)
        fgap = float(ctx.get("female_labor_gap") or 0.4)

        return {
            "wealth_low": _clamp(1.0 - wealth / 3000.0),
            "pull_high": _clamp(pull / 10.0),
            "network_thin": _clamp(1.0 - net / 5.0),
            "network": _clamp(net / 5.0),
            "border_hard": _clamp(border),
            "risk_high": _clamp(risk_t),
            "isolation": _clamp((1.0 - net / 3.0) * (0.5 + 0.5 * fgap)),
            "remit_pressure": _clamp(remit),
            "conflict": _clamp(conflict),
            "governance_weak": _clamp(gov_weak),
            "digital": _clamp(digital),
            "recruit_density": _clamp(recruit),
            "sex_demand": _clamp(sex_d),
            "labor_demand": _clamp(labor_d),
            "minor": 1.0 if age < 18 else 0.0,
            "age_norm": _clamp(age / 80.0),
            "hub_role": 1.0 if any(
                r in (ctx.get("role") or []) for r in ("hub", "border_hub", "transit", "destination")
            ) else 0.0,
            "known_tag": 1.0
            if str(ctx.get("known_involvement") or "") in ("confirmed_high", "elevated")
            else 0.0,
            "opacity": _clamp(float(ctx.get("border_opacity") or border)),
            "cluster_size": _clamp(float(ctx.get("cluster_size") or 1) / 10.0),
            "origin_match": float(ctx.get("origin_match") or 0.0),
        }

    def score_profile(self, profile_id: str, features: dict[str, float]) -> float:
        agent = self.model["agents"].get(profile_id)
        if not agent:
            return 0.0
        w = agent.get("weights") or {}
        if not w:
            return 0.0
        num = 0.0
        den = 0.0
        for k, wt in w.items():
            wt = float(wt)
            num += wt * float(features.get(k, 0.0))
            den += abs(wt)
        return _clamp(num / den if den else 0.0)

    # ── synthetic training corpus ────────────────────────────────────

    def _synthetic_people(self, n: int = 80, rng: random.Random | None = None) -> list[dict[str, Any]]:
        rng = rng or random.Random(42)
        people = []
        for i in range(n):
            # mix of safe and vulnerable archetypes
            kind = rng.choice(
                ["safe", "safe", "low_wealth_pull", "isolated", "remit", "conflict", "minor"]
            )
            if kind == "safe":
                p = {
                    "id": f"syn_safe_{i}",
                    "base_wealth": rng.uniform(2000, 5000),
                    "network_size": rng.randint(2, 8),
                    "origin_jobs": rng.uniform(30, 80),
                    "dest_jobs": rng.uniform(40, 70),
                    "border_friction": rng.uniform(0.2, 0.5),
                    "risk_tolerance": rng.uniform(0.2, 0.5),
                    "dob": f"{rng.randint(1975, 2000)}-01-01",
                    "_label_profiles": [],
                }
            elif kind == "low_wealth_pull":
                p = {
                    "id": f"syn_lwp_{i}",
                    "base_wealth": rng.uniform(200, 1100),
                    "network_size": rng.randint(0, 2),
                    "origin_jobs": rng.uniform(5, 18),
                    "dest_jobs": rng.uniform(70, 95),
                    "border_friction": rng.uniform(0.5, 0.85),
                    "risk_tolerance": rng.uniform(0.55, 0.9),
                    "dob": f"{rng.randint(1985, 2004)}-01-01",
                    "_label_profiles": ["vuln_low_wealth_high_pull"],
                }
            elif kind == "isolated":
                p = {
                    "id": f"syn_iso_{i}",
                    "base_wealth": rng.uniform(400, 1600),
                    "network_size": rng.randint(0, 1),
                    "origin_jobs": rng.uniform(8, 25),
                    "dest_jobs": rng.uniform(65, 90),
                    "border_friction": rng.uniform(0.4, 0.7),
                    "risk_tolerance": rng.uniform(0.5, 0.85),
                    "dob": f"{rng.randint(1988, 2005)}-01-01",
                    "_label_profiles": ["vuln_isolated_female_coded", "vuln_low_wealth_high_pull"],
                }
            elif kind == "remit":
                p = {
                    "id": f"syn_rem_{i}",
                    "base_wealth": rng.uniform(500, 1800),
                    "network_size": rng.randint(1, 4),
                    "origin_jobs": rng.uniform(10, 30),
                    "dest_jobs": rng.uniform(60, 90),
                    "border_friction": rng.uniform(0.35, 0.65),
                    "risk_tolerance": rng.uniform(0.4, 0.7),
                    "remittance_dependency": rng.uniform(0.6, 0.9),
                    "dob": f"{rng.randint(1980, 2000)}-01-01",
                    "_label_profiles": ["vuln_remittance_pressure"],
                }
            elif kind == "conflict":
                p = {
                    "id": f"syn_conf_{i}",
                    "base_wealth": rng.uniform(300, 2000),
                    "network_size": rng.randint(0, 3),
                    "origin_jobs": rng.uniform(5, 25),
                    "dest_jobs": rng.uniform(50, 85),
                    "border_friction": rng.uniform(0.5, 0.9),
                    "risk_tolerance": rng.uniform(0.45, 0.8),
                    "dob": f"{rng.randint(1985, 2003)}-01-01",
                    "_label_profiles": ["vuln_conflict_displaced"],
                    "_context": {"conflict_index": rng.uniform(0.6, 0.9), "rule_of_law": 0.2},
                }
            else:  # minor — protection only
                p = {
                    "id": f"syn_min_{i}",
                    "base_wealth": rng.uniform(0, 800),
                    "network_size": rng.randint(0, 2),
                    "origin_jobs": 5,
                    "dest_jobs": 70,
                    "border_friction": 0.6,
                    "risk_tolerance": 0.7,
                    "dob": f"{rng.randint(2010, 2020)}-01-01",
                    "age": rng.randint(12, 17),
                    "_label_profiles": ["vuln_child_protection"],
                }
            people.append(p)
        return people

    def _context_for_person(self, person: dict[str, Any]) -> dict[str, Any]:
        ctx = dict(person.get("_context") or {})
        if person.get("remittance_dependency") is not None:
            ctx["remittance_dependency"] = person["remittance_dependency"]
        # attach nearest high-risk place context if engine available
        if self.risk and person.get("birth_location"):
            hits = self.risk.resolve_area(query=str(person["birth_location"]))
            if hits:
                node = hits[0]
                sc = self.risk.score_node(node)
                ctx.update(
                    {
                        "border_opacity": node.get("border_opacity"),
                        "conflict_index": node.get("conflict_index"),
                        "rule_of_law": node.get("rule_of_law"),
                        "remittance_dependency": node.get("remittance_dependency"),
                        "digital_recruitment_exposure": node.get("digital_recruitment_exposure"),
                        "recruitment_network_density": node.get("recruitment_network_density"),
                        "sex_industry_demand_proxy": node.get("sex_industry_demand_proxy"),
                        "labor_demand_proxy": node.get("labor_demand_proxy"),
                        "female_labor_gap": node.get("female_labor_gap"),
                        "known_involvement": node.get("known_involvement"),
                        "role": node.get("role"),
                        "governance_weak": sc["components"].get("governance_weak"),
                    }
                )
        return ctx

    def train(
        self,
        epochs: int = 8,
        synthetic: int = 100,
        real_people: list[dict[str, Any]] | None = None,
        lr: float = 0.08,
        seed: int = 7,
    ) -> dict[str, Any]:
        """
        Online training: nudge agent weights toward labeled synthetic archetypes
        and weak labels from real people (heuristic).
        """
        rng = random.Random(seed)
        people = self._synthetic_people(synthetic, rng)
        if real_people:
            for p in real_people:
                rp = dict(p)
                # weak label from heuristics
                rp["_label_profiles"] = self._weak_labels(rp)
                people.append(rp)

        stats = {pid: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for pid in self.model["agents"]}

        for ep in range(epochs):
            rng.shuffle(people)
            for person in people:
                labels = set(person.get("_label_profiles") or [])
                ctx = self._context_for_person(person)
                feats = self.person_features(person, ctx)
                for pid, agent in self.model["agents"].items():
                    # only train person-category agents on person samples
                    prof = next((x for x in self.profiles if x["id"] == pid), {})
                    if prof.get("category") not in ("vulnerable_person", None):
                        # group/network agents trained separately below
                        if prof.get("category") != "vulnerable_person":
                            continue
                    score = self.score_profile(pid, feats)
                    thr = float(agent["threshold"])
                    pred = score >= thr
                    truth = pid in labels
                    st = stats[pid]
                    if pred and truth:
                        st["tp"] += 1
                        target = 1.0
                    elif pred and not truth:
                        st["fp"] += 1
                        target = 0.0
                    elif (not pred) and truth:
                        st["fn"] += 1
                        target = 1.0
                    else:
                        st["tn"] += 1
                        target = 0.0

                    # weight update toward target score
                    err = target - score
                    for k in list(agent["weights"].keys()):
                        fk = float(feats.get(k, 0.0))
                        agent["weights"][k] = float(agent["weights"][k]) + lr * err * fk
                        agent["weights"][k] = _clamp(agent["weights"][k], 0.02, 1.5)
                    # normalize weights lightly
                    s = sum(abs(float(v)) for v in agent["weights"].values()) or 1.0
                    for k in agent["weights"]:
                        agent["weights"][k] = float(agent["weights"][k]) / s * len(agent["weights"]) * 0.25
                        agent["weights"][k] = _clamp(agent["weights"][k], 0.02, 1.2)

                    agent["samples_seen"] = int(agent.get("samples_seen", 0)) + 1
                    # adaptive threshold
                    if truth and score < thr:
                        agent["threshold"] = _clamp(thr - 0.01, 0.25, 0.85)
                    if (not truth) and score >= thr:
                        agent["threshold"] = _clamp(thr + 0.005, 0.25, 0.85)

                    agent["trained_epochs"] = ep + 1

            # train group/network agents using place context from risk engine
            self._train_place_agents(lr=lr * 0.5)

        # finalize accuracy
        for pid, agent in self.model["agents"].items():
            st = stats.get(pid) or {}
            tot = st.get("tp", 0) + st.get("tn", 0) + st.get("fp", 0) + st.get("fn", 0)
            acc = (st.get("tp", 0) + st.get("tn", 0)) / tot if tot else 0.5
            agent["accuracy_ema"] = round(0.3 * float(agent.get("accuracy_ema", 0.5)) + 0.7 * acc, 4)
            agent["true_pos_ema"] = st.get("tp", 0)
            agent["false_pos_ema"] = st.get("fp", 0)
            agent["last_stats"] = st

        self.model["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.model["training_log"].append(
            {
                "ts": self.model["updated_at"],
                "epochs": epochs,
                "synthetic": synthetic,
                "real": len(real_people or []),
            }
        )
        self.model["training_log"] = self.model["training_log"][-50:]
        self.save()
        return {
            "agents": len(self.model["agents"]),
            "epochs": epochs,
            "samples": len(people),
            "per_agent": {
                pid: {
                    "accuracy_ema": a.get("accuracy_ema"),
                    "threshold": round(float(a.get("threshold", 0.5)), 3),
                    "samples_seen": a.get("samples_seen"),
                    "stats": a.get("last_stats"),
                }
                for pid, a in self.model["agents"].items()
            },
        }

    def _train_place_agents(self, lr: float = 0.04) -> None:
        if not self.risk:
            return
        for node in self.risk.nodes:
            sc = self.risk.score_node(node)
            ctx = {
                **node,
                "governance_weak": sc["components"].get("governance_weak"),
                "cluster_size": 4 if "hub" in (node.get("role") or []) else 2,
                "origin_match": 1.0,
            }
            # synthetic "person at place"
            person = {
                "base_wealth": 1000 * (1.1 - float(node.get("poverty_index") or 0.5)),
                "network_size": 1 if float(node.get("recruitment_network_density") or 0) > 0.6 else 3,
                "origin_jobs": 15,
                "dest_jobs": 40 + 50 * float(node.get("labor_demand_proxy") or 0.4),
                "border_friction": float(node.get("border_opacity") or 0.4),
                "risk_tolerance": 0.6,
            }
            feats = self.person_features(person, ctx)
            known = str(node.get("known_involvement") or "") in ("confirmed_high", "elevated")
            for pid, agent in self.model["agents"].items():
                prof = next((x for x in self.profiles if x["id"] == pid), {})
                if prof.get("category") not in ("risk_group", "network_pattern"):
                    continue
                # weak labels from place tags
                truth = False
                if pid == "net_recruiter_hub_pattern":
                    truth = float(node.get("recruitment_network_density") or 0) >= 0.7 and known
                elif pid == "net_demand_city_sink":
                    truth = (
                        float(node.get("sex_industry_demand_proxy") or 0) >= 0.55
                        or float(node.get("labor_demand_proxy") or 0) >= 0.7
                    ) and "destination" in (node.get("role") or [])
                elif pid == "grp_border_transit_wave":
                    truth = any(
                        r in (node.get("role") or [])
                        for r in ("transit", "border_hub", "landing")
                    ) and float(node.get("border_opacity") or 0) >= 0.5
                elif pid == "grp_digital_lure_cohort":
                    truth = float(node.get("digital_recruitment_exposure") or 0) >= 0.65
                elif pid == "grp_recruitment_cluster":
                    truth = float(node.get("recruitment_network_density") or 0) >= 0.65

                score = self.score_profile(pid, feats)
                target = 1.0 if truth else 0.0
                err = target - score
                for k in list(agent["weights"].keys()):
                    agent["weights"][k] = _clamp(
                        float(agent["weights"][k]) + lr * err * float(feats.get(k, 0.0)),
                        0.02,
                        1.5,
                    )
                agent["samples_seen"] = int(agent.get("samples_seen", 0)) + 1

    def _weak_labels(self, person: dict[str, Any]) -> list[str]:
        labels = []
        wealth = float(person.get("base_wealth") or 1500)
        net = int(person.get("network_size") or 0)
        dest = float(person.get("dest_jobs") or 50)
        origin = float(person.get("origin_jobs") or 10)
        age = person.get("age") or _age_from_dob(person.get("dob"))
        if age is not None and int(age) < 18:
            labels.append("vuln_child_protection")
        if wealth < 1200 and dest >= 70 and origin <= 20:
            labels.append("vuln_low_wealth_high_pull")
        if net <= 1 and wealth < 1800 and dest >= 60:
            labels.append("vuln_isolated_female_coded")
        if float(person.get("remittance_dependency") or 0) >= 0.55:
            labels.append("vuln_remittance_pressure")
        return labels

    # ── detection ────────────────────────────────────────────────────

    def scan_people(
        self,
        people: list[dict[str, Any]],
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        # group clustering by birth_location / origin
        by_origin: dict[str, list[dict[str, Any]]] = {}
        for p in people:
            key = str(p.get("birth_location") or p.get("origin_location_id") or "unknown")
            by_origin.setdefault(key, []).append(p)

        for p in people:
            ctx = self._context_for_person(p)
            origin_key = str(p.get("birth_location") or p.get("origin_location_id") or "unknown")
            cluster = by_origin.get(origin_key) or [p]
            ctx["cluster_size"] = len(cluster)
            ctx["origin_match"] = 1.0 if len(cluster) >= 2 else 0.0
            feats = self.person_features(p, ctx)
            matched = []
            for pid, agent in self.model["agents"].items():
                prof = next((x for x in self.profiles if x["id"] == pid), {})
                score = self.score_profile(pid, feats)
                thr = float(agent.get("threshold") or 0.55)
                if min_score is not None:
                    thr = max(thr, min_score)
                if score >= thr:
                    matched.append(
                        {
                            "profile_id": pid,
                            "label": agent.get("label"),
                            "category": agent.get("category"),
                            "priority": prof.get("priority") or agent.get("priority"),
                            "score": round(score, 4),
                            "threshold": thr,
                            "alerts": prof.get("alerts") or [],
                            "response": prof.get("response"),
                        }
                    )
            if matched:
                matched.sort(key=lambda m: m["score"], reverse=True)
                hits.append(
                    {
                        "person_id": p.get("id"),
                        "name": p.get("name") or p.get("id"),
                        "features_snapshot": {
                            "base_wealth": p.get("base_wealth"),
                            "network_size": p.get("network_size"),
                            "dest_jobs": p.get("dest_jobs"),
                            "origin_jobs": p.get("origin_jobs"),
                            "birth_location": p.get("birth_location"),
                        },
                        "matches": matched,
                        "top_priority": matched[0]["priority"],
                        "top_score": matched[0]["score"],
                    }
                )
        hits.sort(
            key=lambda h: (
                0 if h["top_priority"] == "critical" else 1 if h["top_priority"] == "high" else 2,
                -float(h["top_score"]),
            )
        )
        return hits

    def scan_places(self) -> list[dict[str, Any]]:
        if not self.risk:
            return []
        hits = []
        for node in self.risk.nodes:
            sc = self.risk.score_node(node)
            ctx = {
                **node,
                "governance_weak": sc["components"].get("governance_weak"),
                "cluster_size": 5,
                "origin_match": 1.0,
            }
            person = {
                "id": node["id"],
                "name": node["name"],
                "base_wealth": 1200,
                "network_size": 1,
                "origin_jobs": 12,
                "dest_jobs": 50 + 40 * float(node.get("labor_demand_proxy") or 0.4),
                "border_friction": float(node.get("border_opacity") or 0.4),
                "risk_tolerance": 0.6,
            }
            feats = self.person_features(person, ctx)
            matched = []
            for pid, agent in self.model["agents"].items():
                prof = next((x for x in self.profiles if x["id"] == pid), {})
                if prof.get("category") not in ("risk_group", "network_pattern"):
                    continue
                score = self.score_profile(pid, feats)
                thr = float(agent.get("threshold") or 0.55)
                if score >= thr:
                    matched.append(
                        {
                            "profile_id": pid,
                            "label": agent.get("label"),
                            "category": agent.get("category"),
                            "priority": prof.get("priority"),
                            "score": round(score, 4),
                            "alerts": prof.get("alerts") or [],
                            "response": prof.get("response"),
                        }
                    )
            if matched:
                matched.sort(key=lambda m: m["score"], reverse=True)
                hits.append(
                    {
                        "place_id": node["id"],
                        "name": node["name"],
                        "country": node["country"],
                        "known_involvement": node.get("known_involvement"),
                        "matches": matched,
                        "risk_overall": sc["overall_0_100"],
                    }
                )
        hits.sort(key=lambda h: -float(h["matches"][0]["score"]))
        return hits

    def save(self) -> Path:
        STATE.mkdir(parents=True, exist_ok=True)
        MODEL_PATH.write_text(json.dumps(self.model, indent=2), encoding="utf-8")
        return MODEL_PATH

    def save_hits(self, people_hits: list[dict], place_hits: list[dict]) -> Path:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "disclaimer": (
                "Protection-oriented watch hits. Not proof of criminality. "
                "Do not use to harass individuals. Minors → child-protection only."
            ),
            "people_hits": people_hits,
            "place_hits": place_hits,
            "agent_count": len(self.model.get("agents") or {}),
        }
        HITS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        REPORT_MD.write_text(format_watch_md(payload, self.model), encoding="utf-8")
        return HITS_PATH


def format_watch_md(payload: dict[str, Any], model: dict[str, Any]) -> str:
    lines = [
        "# Watch Agent Report",
        "",
        f"_Generated: `{payload.get('generated_at')}`_",
        "",
        "> " + str(payload.get("disclaimer") or ""),
        "",
        "## Trained agents",
        "",
        "| Profile | Category | Priority | Threshold | Accuracy | Samples |",
        "|---------|----------|----------|----------:|---------:|--------:|",
    ]
    for pid, a in (model.get("agents") or {}).items():
        lines.append(
            f"| {a.get('label')} | {a.get('category')} | {a.get('priority')} | "
            f"{float(a.get('threshold', 0)):.2f} | {float(a.get('accuracy_ema', 0)):.2f} | "
            f"{a.get('samples_seen', 0)} |"
        )
    lines += ["", "## People / group hits", ""]
    ph = payload.get("people_hits") or []
    if not ph:
        lines.append("_No person hits above threshold._")
    for h in ph:
        m0 = (h.get("matches") or [{}])[0]
        lines.append(
            f"- **{h.get('name')}** (`{h.get('person_id')}`) — "
            f"{m0.get('label')} score={m0.get('score')} [{m0.get('priority')}]  \n"
            f"  alerts: {', '.join(m0.get('alerts') or [])}  \n"
            f"  response: {m0.get('response')}"
        )
    lines += ["", "## Place / network hits", ""]
    pl = payload.get("place_hits") or []
    if not pl:
        lines.append("_No place hits._")
    for h in pl[:20]:
        m0 = (h.get("matches") or [{}])[0]
        lines.append(
            f"- **{h.get('name')}** ({h.get('country')}) known={h.get('known_involvement')} — "
            f"{m0.get('label')} score={m0.get('score')}"
        )
    lines.append("")
    return "\n".join(lines)


def load_real_people() -> list[dict[str, Any]]:
    path = STATE / "resolved_persons.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return list(rows) if isinstance(rows, list) else []
    except (json.JSONDecodeError, OSError):
        return []
