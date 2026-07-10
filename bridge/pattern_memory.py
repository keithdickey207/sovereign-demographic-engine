#!/usr/bin/env python3
"""
Pattern memory — local online learning for demographic / remittance behaviour.

Stores feature-bucketed outcomes so agents evolve:
  - migration propensity (gravity weights + decision thresholds)
  - remittance fraction (how much capital returns home)
  - action priors (MIGRATE / STAY / TRANSFER_CAPITAL)

No cloud. State lives in state/pattern_memory.json and grows across runs.
"""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
MEMORY_PATH = STATE_DIR / "pattern_memory.json"

# Default gravity coefficients (α jobs, β network, γ friction, δ wealth)
DEFAULT_WEIGHTS = {
    "alpha": 1.0,
    "beta": 1.2,
    "gamma": 2.0,
    "delta": 0.5,
    "sigmoid_scale": 0.15,
    "sigmoid_center": 20.0,
}

DEFAULT_POLICY = {
    "migrate_threshold": 0.55,  # confidence floor for MIGRATE
    "transfer_threshold": 0.50,
    "remittance_fraction": 0.15,
    "max_remittances": 4,
    "risk_bias": 0.0,  # added to agent risk_tolerance comparison (negative = more migrate)
}

# Learning rates (EMA)
LR_WEIGHTS = 0.08
LR_POLICY = 0.05
LR_BUCKET = 0.12
MAX_EPISODES = 500


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def feature_bucket(
    wealth: float,
    network_size: int,
    origin_jobs: float,
    dest_jobs: float,
    border_friction: float,
) -> str:
    """Discretize agent features into a stable pattern key."""
    w = "low" if wealth < 1200 else ("mid" if wealth < 2200 else "high")
    n = "solo" if network_size <= 1 else ("net" if network_size <= 3 else "hub")
    pull = dest_jobs / max(origin_jobs, 1.0)
    p = "weak" if pull < 3 else ("mod" if pull < 6 else "strong")
    f = "open" if border_friction < 0.5 else ("mid" if border_friction < 0.75 else "hard")
    return f"w={w}|n={n}|p={p}|f={f}"


class PatternMemory:
    """
    Online pattern store.

    Episodes record what agents did and how capital moved.
    Weights / policy drift toward higher-reward corridors.
    """

    def __init__(self, path: Path = MEMORY_PATH) -> None:
        self.path = path
        self._lock = RLock()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "episode_count": 0,
            "weights": dict(DEFAULT_WEIGHTS),
            "policy": dict(DEFAULT_POLICY),
            "buckets": {},  # pattern_key -> stats
            "action_priors": {
                "MIGRATE": 0.34,
                "STAY": 0.33,
                "TRANSFER_CAPITAL": 0.33,
            },
            "episodes": [],  # recent ring buffer
            "metrics": {
                "avg_remit_yield": 0.0,
                "avg_migrate_success": 0.0,
                "origin_inflow_total": 0.0,
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            base = self._default()
            base.update({k: v for k, v in raw.items() if k in base})
            # deep-merge known nests
            for key in ("weights", "policy", "action_priors", "metrics"):
                if isinstance(raw.get(key), dict):
                    base[key] = {**base[key], **raw[key]}
            if isinstance(raw.get("buckets"), dict):
                base["buckets"] = raw["buckets"]
            if isinstance(raw.get("episodes"), list):
                base["episodes"] = raw["episodes"][-MAX_EPISODES:]
            return base
        except (json.JSONDecodeError, OSError, TypeError):
            return self._default()

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # ── reads used by inference / spatial ────────────────────────────

    def weights(self) -> dict[str, float]:
        with self._lock:
            return {k: float(v) for k, v in self.data["weights"].items()}

    def policy(self) -> dict[str, float]:
        with self._lock:
            return {k: float(v) for k, v in self.data["policy"].items()}

    def remittance_fraction(self) -> float:
        return _clamp(float(self.policy().get("remittance_fraction", 0.15)), 0.05, 0.40)

    def migrate_threshold(self) -> float:
        return _clamp(float(self.policy().get("migrate_threshold", 0.55)), 0.35, 0.85)

    def risk_bias(self) -> float:
        return _clamp(float(self.policy().get("risk_bias", 0.0)), -0.25, 0.25)

    def gravity_probability(
        self,
        economic_attraction: float,
        social_graph_weight: float,
        border_friction: float,
        base_wealth: float,
    ) -> float:
        w = self.weights()
        alpha = w.get("alpha", 1.0)
        beta = w.get("beta", 1.2)
        gamma = w.get("gamma", 2.0)
        delta = w.get("delta", 0.5)
        scale = w.get("sigmoid_scale", 0.15)
        center = w.get("sigmoid_center", 20.0)
        numerator = (alpha * economic_attraction) + (beta * social_graph_weight)
        wealth_term = delta * math.log1p(max(base_wealth, 0.0))
        denominator = (gamma * max(border_friction, 1e-6)) + wealth_term
        raw = numerator / max(denominator, 1e-9)
        return 1.0 / (1.0 + math.exp(-scale * (raw - center)))

    def recommend_action(
        self,
        wealth: float,
        network_size: int,
        origin_jobs: float,
        dest_jobs: float,
        border_friction: float,
        risk_tolerance: float = 0.75,
    ) -> dict[str, Any]:
        """
        Pattern-informed offline decision (used as primary heuristic and
        as prior when Ollama is available).
        """
        bucket = feature_bucket(
            wealth, network_size, origin_jobs, dest_jobs, border_friction
        )
        with self._lock:
            stats = self.data["buckets"].get(bucket) or {}
            priors = dict(self.data["action_priors"])

        # Blend global priors with bucket-local action rates
        local_n = float(stats.get("n", 0))
        if local_n >= 3:
            for act in ("MIGRATE", "STAY", "TRANSFER_CAPITAL"):
                rate = float(stats.get(f"rate_{act}", 0.0))
                # shrink toward global prior when sample is small
                mix = min(1.0, local_n / 20.0)
                priors[act] = (1.0 - mix) * priors[act] + mix * rate

        g = self.gravity_probability(
            economic_attraction=dest_jobs,
            social_graph_weight=float(network_size),
            border_friction=border_friction,
            base_wealth=wealth,
        )
        thr = self.migrate_threshold()
        bias = self.risk_bias()
        effective_risk = risk_tolerance + bias

        # Score actions
        pull = dest_jobs / max(origin_jobs, 1.0)
        scores = {
            "MIGRATE": priors["MIGRATE"] * (0.4 + 0.6 * g)
            + (0.15 if pull > 4 else 0.0)
            + (0.1 if network_size >= 2 else 0.0),
            "TRANSFER_CAPITAL": priors["TRANSFER_CAPITAL"]
            * (0.5 + 0.3 * min(wealth / 3000.0, 1.0))
            + (0.12 if network_size >= 1 else 0.0),
            "STAY": priors["STAY"] * (0.5 + (0.3 if g < effective_risk else 0.05)),
        }

        # Soft push: high gravity vs risk → migrate
        if g > effective_risk:
            scores["MIGRATE"] += 0.25 * (g - effective_risk)

        action = max(scores, key=scores.get)
        # Confidence from margin + sample support
        ordered = sorted(scores.values(), reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
        conf = _clamp(0.45 + margin + min(local_n, 15) * 0.01, 0.4, 0.95)

        # Policy thresholds
        if action == "MIGRATE" and conf < thr:
            action = "STAY" if scores["STAY"] >= scores["TRANSFER_CAPITAL"] else "TRANSFER_CAPITAL"
            conf = max(0.5, conf * 0.9)

        return {
            "action": action,
            "confidence": round(conf, 4),
            "source": "pattern_memory",
            "pattern_bucket": bucket,
            "gravity_p": round(g, 4),
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "sample_n": int(local_n),
        }

    # ── writes / learning ────────────────────────────────────────────

    def observe_decision(
        self,
        agent_id: str,
        action: str,
        confidence: float,
        features: dict[str, Any],
        source: str = "unknown",
    ) -> None:
        """Log a decision tick (pre-outcome)."""
        action = str(action).upper()
        if action not in ("MIGRATE", "STAY", "TRANSFER_CAPITAL"):
            action = "STAY"
        bucket = feature_bucket(
            float(features.get("base_wealth", features.get("wealth", 1500))),
            int(features.get("network_size", 0)),
            float(features.get("origin_jobs", 10)),
            float(features.get("dest_jobs", 50)),
            float(features.get("border_friction", 0.6)),
        )
        with self._lock:
            b = self.data["buckets"].setdefault(
                bucket,
                {
                    "n": 0,
                    "rate_MIGRATE": 0.0,
                    "rate_STAY": 0.0,
                    "rate_TRANSFER_CAPITAL": 0.0,
                    "reward_ema": 0.0,
                    "migrate_success": 0.0,
                    "remit_yield_ema": 0.0,
                },
            )
            n = float(b["n"])
            # Online frequency update
            for act in ("MIGRATE", "STAY", "TRANSFER_CAPITAL"):
                key = f"rate_{act}"
                target = 1.0 if act == action else 0.0
                b[key] = (n * float(b.get(key, 0.0)) + target) / (n + 1.0)
            b["n"] = int(n + 1)

            # Global action priors (slow drift)
            priors = self.data["action_priors"]
            for act in priors:
                target = 1.0 if act == action else 0.0
                priors[act] = (1.0 - LR_POLICY) * float(priors[act]) + LR_POLICY * target
            # renormalize
            s = sum(float(v) for v in priors.values()) or 1.0
            for act in priors:
                priors[act] = float(priors[act]) / s

            self.data["episodes"].append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "kind": "decision",
                    "agent_id": agent_id,
                    "action": action,
                    "confidence": confidence,
                    "bucket": bucket,
                    "source": source,
                    "features": {
                        "wealth": float(features.get("base_wealth", features.get("wealth", 0))),
                        "network_size": int(features.get("network_size", 0)),
                        "dest_jobs": float(features.get("dest_jobs", 0)),
                        "origin_jobs": float(features.get("origin_jobs", 0)),
                        "border_friction": float(features.get("border_friction", 0.6)),
                    },
                }
            )
            self.data["episodes"] = self.data["episodes"][-MAX_EPISODES:]
            self.data["episode_count"] = int(self.data.get("episode_count", 0)) + 1
        self.save()

    def observe_episode(
        self,
        agent_id: str,
        migrated: bool,
        total_remitted: float,
        start_wealth: float,
        remittance_count: int,
        features: dict[str, Any] | None = None,
        transit_time_s: float = 0.0,
    ) -> dict[str, Any]:
        """
        Close a full agent lifecycle episode and update weights/policy.

        Reward: remittance yield + completion bonus − friction penalty.
        """
        features = features or {}
        start_wealth = max(float(start_wealth), 1.0)
        yield_frac = _clamp(float(total_remitted) / start_wealth, 0.0, 1.0)
        reward = yield_frac
        if migrated:
            reward += 0.25
        if remittance_count >= 4:
            reward += 0.15
        if transit_time_s > 0:
            # Prefer moderate transit (not stuck)
            reward += 0.05 if 5.0 <= transit_time_s <= 40.0 else -0.05
        reward = _clamp(reward, 0.0, 1.5)

        bucket = feature_bucket(
            float(features.get("base_wealth", start_wealth)),
            int(features.get("network_size", 0)),
            float(features.get("origin_jobs", 10)),
            float(features.get("dest_jobs", 50)),
            float(features.get("border_friction", 0.6)),
        )

        with self._lock:
            b = self.data["buckets"].setdefault(
                bucket,
                {
                    "n": 0,
                    "rate_MIGRATE": 0.0,
                    "rate_STAY": 0.0,
                    "rate_TRANSFER_CAPITAL": 0.0,
                    "reward_ema": 0.0,
                    "migrate_success": 0.0,
                    "remit_yield_ema": 0.0,
                },
            )
            b["reward_ema"] = (1.0 - LR_BUCKET) * float(b.get("reward_ema", 0)) + LR_BUCKET * reward
            b["migrate_success"] = (
                (1.0 - LR_BUCKET) * float(b.get("migrate_success", 0))
                + LR_BUCKET * (1.0 if migrated else 0.0)
            )
            b["remit_yield_ema"] = (
                (1.0 - LR_BUCKET) * float(b.get("remit_yield_ema", 0)) + LR_BUCKET * yield_frac
            )

            # Adaptive gravity weights: if migrated+high yield, boost pull factors
            w = self.data["weights"]
            pol = self.data["policy"]
            if migrated and reward > 0.6:
                w["alpha"] = _clamp(float(w["alpha"]) + LR_WEIGHTS * 0.15, 0.4, 2.5)
                w["beta"] = _clamp(float(w["beta"]) + LR_WEIGHTS * 0.12, 0.4, 2.5)
                w["gamma"] = _clamp(float(w["gamma"]) - LR_WEIGHTS * 0.08, 0.5, 3.5)
                # Successful migrants remit a bit more aggressively next time
                pol["remittance_fraction"] = _clamp(
                    float(pol["remittance_fraction"]) + LR_POLICY * 0.02, 0.08, 0.35
                )
                pol["risk_bias"] = _clamp(
                    float(pol["risk_bias"]) - LR_POLICY * 0.02, -0.25, 0.25
                )
            elif migrated and reward < 0.35:
                # Poor remittance yield after migrate → slightly more conservative
                pol["remittance_fraction"] = _clamp(
                    float(pol["remittance_fraction"]) - LR_POLICY * 0.015, 0.08, 0.35
                )
                w["gamma"] = _clamp(float(w["gamma"]) + LR_WEIGHTS * 0.05, 0.5, 3.5)
            elif not migrated:
                # Stayers: if pull was strong, lower migrate threshold slightly
                pull = float(features.get("dest_jobs", 50)) / max(
                    float(features.get("origin_jobs", 10)), 1.0
                )
                if pull > 5:
                    pol["migrate_threshold"] = _clamp(
                        float(pol["migrate_threshold"]) - LR_POLICY * 0.01, 0.35, 0.85
                    )

            m = self.data["metrics"]
            m["avg_remit_yield"] = (
                (1.0 - LR_POLICY) * float(m.get("avg_remit_yield", 0)) + LR_POLICY * yield_frac
            )
            m["avg_migrate_success"] = (
                (1.0 - LR_POLICY) * float(m.get("avg_migrate_success", 0))
                + LR_POLICY * (1.0 if migrated else 0.0)
            )
            m["origin_inflow_total"] = float(m.get("origin_inflow_total", 0)) + float(
                total_remitted
            )

            self.data["episodes"].append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "kind": "episode",
                    "agent_id": agent_id,
                    "migrated": migrated,
                    "total_remitted": round(float(total_remitted), 2),
                    "start_wealth": round(start_wealth, 2),
                    "yield_frac": round(yield_frac, 4),
                    "reward": round(reward, 4),
                    "remittance_count": remittance_count,
                    "transit_time_s": round(transit_time_s, 2),
                    "bucket": bucket,
                }
            )
            self.data["episodes"] = self.data["episodes"][-MAX_EPISODES:]
            self.data["episode_count"] = int(self.data.get("episode_count", 0)) + 1
            summary = {
                "agent_id": agent_id,
                "reward": round(reward, 4),
                "yield_frac": round(yield_frac, 4),
                "weights": deepcopy(w),
                "policy": deepcopy(pol),
                "bucket": bucket,
            }
        self.save()
        return summary

    def observe_run(self, metrics: dict[str, Any], agents: list[dict[str, Any]]) -> None:
        """Absorb a full spatial run summary (batch)."""
        for a in agents:
            self.observe_episode(
                agent_id=str(a.get("id") or a.get("agent_id")),
                migrated=bool(a.get("has_migrated") or a.get("state") in ("REMITTING", "SETTLED")),
                total_remitted=float(a.get("total_remitted") or 0),
                start_wealth=float(
                    a.get("start_wealth")
                    or (float(a.get("base_wealth") or 0) + float(a.get("total_remitted") or 0))
                ),
                remittance_count=int(a.get("remittance_count") or 0),
                features={
                    "base_wealth": float(a.get("start_wealth") or a.get("base_wealth") or 1500),
                    "network_size": int(a.get("network_size") or a.get("social_graph_weight") or 0),
                    "origin_jobs": float(a.get("origin_jobs") or 10),
                    "dest_jobs": float(a.get("dest_jobs") or a.get("economic_attraction") or 50),
                    "border_friction": float(a.get("border_friction") or 0.6),
                },
                transit_time_s=float(a.get("time_in_destination") or 0),
            )
        with self._lock:
            self.data["metrics"]["last_run"] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "pulses_spawned": metrics.get("pulses_spawned"),
                "pulses_settled": metrics.get("pulses_settled"),
                "origin_aggregate_wealth": metrics.get("origin_aggregate_wealth"),
            }
        self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self.data)

    def summary_lines(self) -> list[str]:
        with self._lock:
            w = self.data["weights"]
            p = self.data["policy"]
            m = self.data["metrics"]
            n_buckets = len(self.data.get("buckets") or {})
            return [
                f"episodes={self.data.get('episode_count', 0)} buckets={n_buckets}",
                f"weights α={w['alpha']:.3f} β={w['beta']:.3f} γ={w['gamma']:.3f} δ={w['delta']:.3f}",
                f"policy remit_frac={p['remittance_fraction']:.3f} "
                f"migrate_thr={p['migrate_threshold']:.3f} risk_bias={p['risk_bias']:.3f}",
                f"metrics yield={m.get('avg_remit_yield', 0):.3f} "
                f"migrate_ok={m.get('avg_migrate_success', 0):.3f} "
                f"inflow_total={m.get('origin_inflow_total', 0):.1f}",
            ]


# Module-level singleton for simple imports
_memory: PatternMemory | None = None
_mem_lock = RLock()


def get_memory() -> PatternMemory:
    global _memory
    with _mem_lock:
        if _memory is None:
            _memory = PatternMemory()
        return _memory
