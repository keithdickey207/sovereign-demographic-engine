#!/usr/bin/env python3
"""
Sovereign Spatial Engine — custom, local-only agent kinematics.

No Godot. No Unity. No commercial game runtime. No cloud render pipeline.
Gravity-model migration math, capital pulses, and world projection all execute
on this host so the simulation surface never leaves the machine.

Agent lifecycle (state machine):
  STAY → MIGRATING → REMITTING → (optional SETTLED after N remittances)

Arrival uses snap-to-destination when remaining distance ≤ step size so
high-speed compressed transit cannot overshoot the geofence forever.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Literal
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.graph_store import GraphBackend  # noqa: E402
from bridge.pattern_memory import get_memory  # noqa: E402

STATE_DIR = ROOT / "state"
FRAME_PATH = STATE_DIR / "spatial_frame.json"
DECISIONS_PATH = STATE_DIR / "tick_decisions.json"

# Corridor anchors (degrees) — Zacatecas origin → El Paso hub
ORIGIN_LAT, ORIGIN_LON = 22.7709, -102.5832
DEST_LAT, DEST_LON = 31.7619, -106.4850
METERS_PER_DEG_LAT = 111_132.0

API_HOST = os.environ.get("SPATIAL_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("SPATIAL_API_PORT", "8768"))
INFERENCE_URL = os.environ.get("DEMOGRAPHIC_INFERENCE_URL", "http://127.0.0.1:8767")

# Geofence + remittance economy (fraction/max adapted by pattern memory at runtime)
ARRIVAL_THRESHOLD_M = 2_000.0  # 2 km arrival radius (compressed corridor)
REMITTANCE_COOLDOWN_S = 2.5  # sim-seconds between capital pulses
REMITTANCE_FRACTION = 0.15  # default share of agent wealth per pulse
MAX_REMITTANCES = 4  # then SETTLED (stops pulsing)

AgentState = Literal["STAY", "MIGRATING", "REMITTING", "SETTLED"]


def latlon_to_xy(
    lat: float, lon: float, ref_lat: float = ORIGIN_LAT, ref_lon: float = ORIGIN_LON
) -> tuple[float, float]:
    m_lon = METERS_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    x = (lon - ref_lon) * m_lon
    y = (lat - ref_lat) * METERS_PER_DEG_LAT
    return x, y


def xy_to_latlon(
    x: float, y: float, ref_lat: float = ORIGIN_LAT, ref_lon: float = ORIGIN_LON
) -> tuple[float, float]:
    m_lon = METERS_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    lon = ref_lon + x / m_lon
    lat = ref_lat + y / METERS_PER_DEG_LAT
    return lat, lon


ORIGIN_XY = latlon_to_xy(ORIGIN_LAT, ORIGIN_LON)
DEST_XY = latlon_to_xy(DEST_LAT, DEST_LON)
DEFAULT_ORIGIN_ID = "loc_mx_001"


@dataclass
class CapitalPulse:
    agent_id: str
    value: float
    origin: list[float]
    destination: list[float]
    progress: float = 0.0
    alive: bool = True
    settled_on_graph: bool = False  # wealth applied when pulse completes

    def step(self, dt: float, speed: float = 0.85) -> bool:
        """Advance pulse. Returns True the frame it completes (for graph settle)."""
        if not self.alive:
            return False
        self.progress = min(1.0, self.progress + speed * dt)
        if self.progress >= 1.0:
            self.alive = False
            if not self.settled_on_graph:
                self.settled_on_graph = True
                return True
        return False

    def position(self) -> list[float]:
        ox, oy = self.origin
        dx, dy = self.destination
        t = self.progress
        return [ox + (dx - ox) * t, oy + (dy - oy) * t]


@dataclass
class AgentBody:
    agent_id: str
    name: str
    base_wealth: float
    risk_tolerance: float
    social_graph_weight: float
    economic_attraction: float
    border_friction: float
    origin_location_id: str = DEFAULT_ORIGIN_ID
    migration_speed: float = 45_000.0  # meters per sim-second (compressed time)
    state: AgentState = "STAY"
    position: list[float] = field(default_factory=lambda: list(ORIGIN_XY))
    destination: list[float] = field(default_factory=lambda: list(DEST_XY))
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0])
    last_action: str = "STAY"
    last_confidence: float = 0.0
    color: str = "#6b8cae"
    time_in_destination: float = 0.0
    remittance_count: int = 0
    total_remitted: float = 0.0
    remittance_value: float = 0.0
    start_wealth: float = 0.0  # snapshot at run start for learning yield
    transit_time_s: float = 0.0
    episode_logged: bool = False
    origin_jobs: float = 10.0

    @property
    def has_migrated(self) -> bool:
        return self.state in ("REMITTING", "SETTLED")

    def calculate_gravity_model(self) -> float:
        """Gravity pull — coefficients evolve via pattern memory."""
        return get_memory().gravity_probability(
            economic_attraction=self.economic_attraction,
            social_graph_weight=self.social_graph_weight,
            border_friction=self.border_friction,
            base_wealth=self.base_wealth,
        )

    def remittance_fraction(self) -> float:
        return get_memory().remittance_fraction()

    def distance_to_dest(self) -> float:
        dx = self.destination[0] - self.position[0]
        dy = self.destination[1] - self.position[1]
        return math.hypot(dx, dy)

    def begin_migration(self) -> None:
        if self.state == "STAY":
            self.state = "MIGRATING"
            self.color = "#e6a817"
            print(f"[{self.agent_id}] State STAY → MIGRATING")

    def arrive(self) -> None:
        """Snap to destination and enter remittance economy."""
        self.position = list(self.destination)
        self.velocity = [0.0, 0.0]
        self.state = "REMITTING"
        self.color = "#33cc66"
        self.time_in_destination = 0.0
        frac = self.remittance_fraction()
        self.remittance_value = max(self.base_wealth * frac, 1.0)
        print(
            f"[{self.agent_id}] Arrived at destination. "
            f"State changed to REMITTING (pulse=${self.remittance_value:.2f} "
            f"frac={frac:.3f})."
        )

    def move_towards_destination(self, dt: float) -> bool:
        """
        Advance along transit vector.
        Returns True if this step completed arrival (state → REMITTING).
        """
        dist = self.distance_to_dest()
        step = self.migration_speed * dt
        # Snap if inside geofence OR remaining distance ≤ one physics step
        # (prevents permanent overshoot oscillation at high compressed speeds)
        if dist <= max(ARRIVAL_THRESHOLD_M, step):
            self.arrive()
            return True

        inv = 1.0 / dist
        self.velocity = [
            (self.destination[0] - self.position[0]) * inv * self.migration_speed,
            (self.destination[1] - self.position[1]) * inv * self.migration_speed,
        ]
        self.position[0] += self.velocity[0] * dt
        self.position[1] += self.velocity[1] * dt
        self.color = "#e6a817"
        return False


class SovereignSpatialEngine:
    """Private spatial runtime — all math and state stay on-box."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.agents: dict[str, AgentBody] = {}
        self.pulses: list[CapitalPulse] = []
        self.tick = 0
        self.sim_time = 0.0
        self.pulses_spawned = 0
        self.pulses_settled = 0
        self.backend = GraphBackend()
        self._bootstrap_agents()

    def _baseline_wealth_map(self) -> dict[str, float]:
        """Prefer resolved_persons.json so --reset restores pre-remittance capital."""
        path = STATE_DIR / "resolved_persons.json"
        out: dict[str, float] = {}
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                for row in rows:
                    out[str(row["id"])] = float(row.get("base_wealth") or 1500)
            except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
                pass
        return out

    def _bootstrap_agents(self) -> None:
        persons = self.backend.list_persons()
        if not persons:
            persons = [
                {
                    "id": "agent_8472",
                    "name": "Jose_Rojas",
                    "base_wealth": 1500,
                    "risk_tolerance": 0.85,
                    "network_size": 4,
                    "dest_jobs": 85,
                    "border_friction": 0.6,
                    "has_migrated": False,
                    "origin_location_id": DEFAULT_ORIGIN_ID,
                }
            ]
        for p in persons:
            aid = p["id"]
            if aid in self.agents:
                continue
            migrated = bool(p.get("has_migrated"))
            # Resume from graph: already-migrated agents sit at dest in REMITTING.
            # Use --reset for a clean STAY→MIGRATING corridor burn-in.
            if migrated:
                state: AgentState = "REMITTING"
                pos = list(DEST_XY)
                color = "#33cc66"
            else:
                state = "STAY"
                pos = list(ORIGIN_XY)
                color = "#6b8cae"
            wealth = float(p.get("base_wealth") or 1500)
            self.agents[aid] = AgentBody(
                agent_id=aid,
                name=str(p.get("name") or aid),
                base_wealth=wealth,
                risk_tolerance=float(p.get("risk_tolerance") or 0.75),
                social_graph_weight=float(p.get("network_size") or 0),
                economic_attraction=float(p.get("dest_jobs") or 50),
                border_friction=float(p.get("border_friction") or 0.6),
                origin_location_id=str(
                    p.get("origin_location_id") or DEFAULT_ORIGIN_ID
                ),
                state=state,
                position=pos,
                color=color,
                remittance_value=wealth * get_memory().remittance_fraction(),
                start_wealth=wealth,
                origin_jobs=float(p.get("origin_jobs") or 10),
            )

    def reset_for_corridor_run(
        self,
        restore_wealth: bool = True,
        reset_origin_wealth: bool = False,
        force_migrate: bool = False,
    ) -> None:
        """
        Put every agent back at origin in STAY (optionally force MIGRATING).
        Restores wealth from resolved_persons baseline so remittance math is not
        starting from depleted post-SETTLED balances.
        """
        baselines = self._baseline_wealth_map() if restore_wealth else {}
        with self._lock:
            self.pulses.clear()
            self.pulses_spawned = 0
            self.pulses_settled = 0
            self.tick = 0
            self.sim_time = 0.0
            for agent in self.agents.values():
                wealth = baselines.get(agent.agent_id, agent.base_wealth)
                if restore_wealth and agent.agent_id not in baselines:
                    # Heuristic: if depleted, bump to a demo floor
                    if agent.base_wealth < 500:
                        wealth = 1500.0
                agent.base_wealth = float(wealth)
                agent.start_wealth = float(wealth)
                agent.state = "STAY"
                agent.position = list(ORIGIN_XY)
                agent.velocity = [0.0, 0.0]
                agent.color = "#6b8cae"
                agent.time_in_destination = 0.0
                agent.transit_time_s = 0.0
                agent.remittance_count = 0
                agent.total_remitted = 0.0
                agent.episode_logged = False
                agent.remittance_value = agent.base_wealth * get_memory().remittance_fraction()
                agent.last_action = "STAY"
                agent.last_confidence = 0.0
                try:
                    self.backend.update_person(
                        agent.agent_id,
                        {
                            "has_migrated": False,
                            "base_wealth": agent.base_wealth,
                            "total_remitted": 0,
                        },
                    )
                except Exception as exc:
                    print(f"[spatial] reset graph person failed: {exc}")

                if force_migrate:
                    agent.last_action = "MIGRATE"
                    agent.last_confidence = 0.99
                    agent.begin_migration()

            if reset_origin_wealth:
                try:
                    if self.backend.backend == "neo4j" and self.backend.driver:
                        with self.backend.driver.session() as session:
                            session.run(
                                """
                                MATCH (o:Location {id: $id})
                                SET o.aggregate_wealth = 0,
                                    o.remittance_inflow = 0,
                                    o.remittance_events = 0
                                """,
                                id=DEFAULT_ORIGIN_ID,
                            )
                    else:
                        # Drop bloated remittance edges; keep structural edges
                        with self.backend.local._lock:
                            data = self.backend.local._read()
                            data["edges"] = [
                                e
                                for e in data.get("edges") or []
                                if e.get("type") != "TRANSFERRED_CAPITAL"
                                or (
                                    (e.get("props") or {}).get("year") == 2024
                                    and (e.get("props") or {}).get("amount") == 25000
                                )
                            ]
                            for loc_id in (DEFAULT_ORIGIN_ID, "loc_us_001"):
                                node = data["nodes"].get(loc_id)
                                if node:
                                    node["aggregate_wealth"] = 0.0
                                    node["remittance_inflow"] = 0.0
                                    node["remittance_events"] = 0
                            self.backend.local._write(data)
                except Exception as exc:
                    print(f"[spatial] reset origin wealth failed: {exc}")

        print(
            f"[spatial] reset corridor: agents={len(self.agents)} "
            f"force_migrate={force_migrate} restore_wealth={restore_wealth} "
            f"reset_origin={reset_origin_wealth}"
        )

    def all_terminal(self) -> bool:
        """True when every agent is SETTLED and no pulses still in flight."""
        with self._lock:
            if self.pulses:
                return False
            if not self.agents:
                return True
            return all(a.state == "SETTLED" for a in self.agents.values())

    def ingest_decisions(self) -> None:
        if not DECISIONS_PATH.exists():
            return
        try:
            data = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        decisions = data.get("agents") or {}
        states = data.get("agent_states") or {}
        with self._lock:
            for aid, body in self.agents.items():
                # Do not clobber in-flight MIGRATING / REMITTING from stale graph flags
                if aid in states and body.state == "STAY":
                    s = states[aid]
                    body.base_wealth = float(s.get("base_wealth") or body.base_wealth)
                    body.risk_tolerance = float(
                        s.get("risk_tolerance") or body.risk_tolerance
                    )
                    body.social_graph_weight = float(
                        s.get("network_size") or body.social_graph_weight
                    )
                    body.economic_attraction = float(
                        s.get("dest_jobs") or body.economic_attraction
                    )
                    body.border_friction = float(
                        s.get("border_friction") or body.border_friction
                    )
                d = decisions.get(aid) or {}
                body.last_action = str(d.get("action") or body.last_action)
                body.last_confidence = float(
                    d.get("confidence") or body.last_confidence
                )

    def _max_remittances(self) -> int:
        pol = get_memory().policy()
        return max(1, int(pol.get("max_remittances", MAX_REMITTANCES)))

    def _log_episode_if_needed(self, agent: AgentBody) -> None:
        if agent.episode_logged or agent.state != "SETTLED":
            return
        agent.episode_logged = True
        summary = get_memory().observe_episode(
            agent_id=agent.agent_id,
            migrated=True,
            total_remitted=agent.total_remitted,
            start_wealth=agent.start_wealth or (agent.base_wealth + agent.total_remitted),
            remittance_count=agent.remittance_count,
            features={
                "base_wealth": agent.start_wealth or agent.base_wealth,
                "network_size": int(agent.social_graph_weight),
                "origin_jobs": agent.origin_jobs,
                "dest_jobs": agent.economic_attraction,
                "border_friction": agent.border_friction,
            },
            transit_time_s=agent.transit_time_s,
        )
        print(
            f"[learn] episode {agent.agent_id} reward={summary['reward']:.3f} "
            f"yield={summary['yield_frac']:.3f} "
            f"remit_frac→{summary['policy']['remittance_fraction']:.3f}"
        )

    def _spawn_capital_pulse(self, agent: AgentBody) -> None:
        """Fire a remittance pulse and stage graph debit; settle credit on arrival."""
        frac = agent.remittance_fraction()
        amount = max(agent.base_wealth * frac, 1.0)
        # Cap by remaining wealth
        amount = min(amount, max(agent.base_wealth, 0.0))
        if amount <= 0:
            agent.state = "SETTLED"
            agent.color = "#2ecc71"
            print(f"[{agent.agent_id}] Wealth exhausted. State → SETTLED.")
            self._log_episode_if_needed(agent)
            return

        agent.remittance_value = amount
        pulse = CapitalPulse(
            agent_id=agent.agent_id,
            value=amount,
            origin=list(agent.position),
            destination=list(ORIGIN_XY),
        )
        self.pulses.append(pulse)
        self.pulses_spawned += 1
        agent.remittance_count += 1
        agent.total_remitted += amount
        agent.time_in_destination = 0.0

        # Immediate graph write-back: edge + agent debit + origin credit
        try:
            result = self.backend.record_capital_transfer(
                agent.agent_id,
                amount,
                origin_id=agent.origin_location_id,
            )
            agent.base_wealth = float(result.get("agent_wealth", max(0.0, agent.base_wealth - amount)))
            print(
                f"[spatial] pulse#{self.pulses_spawned} {agent.agent_id} "
                f"${amount:.2f} → {agent.origin_location_id} "
                f"(origin_wealth={result.get('origin_wealth')}, "
                f"backend={self.backend.backend})"
            )
        except Exception as exc:
            # Still animate pulse; local wealth adjust
            agent.base_wealth = max(0.0, agent.base_wealth - amount)
            print(f"[spatial] graph write-back failed: {exc}")

        # Optional notify inference daemon (do NOT re-transfer capital)
        try:
            import requests

            requests.post(
                f"{INFERENCE_URL}/api/agent/arrived",
                json={
                    "agent_id": agent.agent_id,
                    "remittance": 0,  # spatial already wrote capital
                    "state": agent.state,
                    "total_remitted": agent.total_remitted,
                },
                timeout=0.5,
            )
        except Exception:
            pass

        if agent.remittance_count >= self._max_remittances() or agent.base_wealth < 50:
            agent.state = "SETTLED"
            agent.color = "#2ecc71"
            print(
                f"[{agent.agent_id}] Remittance cycle complete "
                f"(n={agent.remittance_count}). State → SETTLED."
            )
            self._log_episode_if_needed(agent)

    def _update_agent(self, agent: AgentBody, dt: float) -> None:
        mem = get_memory()
        thr = mem.migrate_threshold()
        risk_bias = mem.risk_bias()

        if agent.state == "STAY":
            probability = agent.calculate_gravity_model()
            force_migrate = (
                agent.last_action == "MIGRATE" and agent.last_confidence >= thr
            )
            effective_risk = agent.risk_tolerance + risk_bias
            if force_migrate or probability > effective_risk:
                agent.begin_migration()
                agent.move_towards_destination(dt)
            else:
                agent.velocity = [0.0, 0.0]
                if agent.last_action == "TRANSFER_CAPITAL":
                    agent.color = "#9b59b6"
                else:
                    agent.color = "#6b8cae"

        elif agent.state == "MIGRATING":
            agent.transit_time_s += dt
            agent.move_towards_destination(dt)
            # First remittance fires immediately on arrival
            if agent.state == "REMITTING":
                self._spawn_capital_pulse(agent)

        elif agent.state == "REMITTING":
            agent.velocity = [0.0, 0.0]
            agent.color = "#9b59b6"
            agent.time_in_destination += dt
            if agent.time_in_destination >= REMITTANCE_COOLDOWN_S:
                self._spawn_capital_pulse(agent)

        elif agent.state == "SETTLED":
            agent.velocity = [0.0, 0.0]
            agent.color = "#2ecc71"
            self._log_episode_if_needed(agent)

    def step(self, dt: float) -> dict[str, Any]:
        self.ingest_decisions()
        with self._lock:
            self.tick += 1
            self.sim_time += dt

            for agent in self.agents.values():
                self._update_agent(agent, dt)

            still_alive: list[CapitalPulse] = []
            for pulse in self.pulses:
                completed = pulse.step(dt)
                if completed:
                    self.pulses_settled += 1
                    print(
                        f"[spatial] pulse settled agent={pulse.agent_id} "
                        f"${pulse.value:.2f} at origin (settled_total={self.pulses_settled})"
                    )
                if pulse.alive:
                    still_alive.append(pulse)
            self.pulses = still_alive

            frame = self._frame_unlocked()
        self._persist(frame)
        return frame

    def _frame_unlocked(self) -> dict[str, Any]:
        agents_out = []
        for a in self.agents.values():
            lat, lon = xy_to_latlon(a.position[0], a.position[1])
            agents_out.append(
                {
                    "id": a.agent_id,
                    "name": a.name,
                    "state": a.state,
                    "x": a.position[0],
                    "y": a.position[1],
                    "lat": lat,
                    "lon": lon,
                    "vx": a.velocity[0],
                    "vy": a.velocity[1],
                    "has_migrated": a.has_migrated,
                    "gravity_p": round(a.calculate_gravity_model(), 4),
                    "risk_tolerance": a.risk_tolerance,
                    "action": a.last_action,
                    "confidence": a.last_confidence,
                    "color": a.color,
                    "base_wealth": round(a.base_wealth, 2),
                    "remittance_count": a.remittance_count,
                    "total_remitted": round(a.total_remitted, 2),
                    "time_in_destination": round(a.time_in_destination, 2),
                }
            )
        pulses_out = [
            {
                "agent_id": p.agent_id,
                "value": p.value,
                "x": p.position()[0],
                "y": p.position()[1],
                "progress": round(p.progress, 3),
            }
            for p in self.pulses
        ]
        origin_stats = self.backend.get_location(DEFAULT_ORIGIN_ID) or {}
        return {
            "engine": "sovereign-spatial",
            "vendor": "none",
            "tick": self.tick,
            "sim_time": round(self.sim_time, 3),
            "backend": self.backend.backend,
            "metrics": {
                "pulses_active": len(self.pulses),
                "pulses_spawned": self.pulses_spawned,
                "pulses_settled": self.pulses_settled,
                "origin_aggregate_wealth": origin_stats.get("aggregate_wealth", 0),
                "origin_remittance_inflow": origin_stats.get("remittance_inflow", 0),
            },
            "corridor": {
                "origin": {"lat": ORIGIN_LAT, "lon": ORIGIN_LON, "xy": list(ORIGIN_XY)},
                "destination": {
                    "lat": DEST_LAT,
                    "lon": DEST_LON,
                    "xy": list(DEST_XY),
                },
            },
            "agents": agents_out,
            "capital_pulses": pulses_out,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._frame_unlocked()

    def _persist(self, frame: dict[str, Any]) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FRAME_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(frame, indent=2), encoding="utf-8")
        tmp.replace(FRAME_PATH)

    def close(self) -> None:
        self.backend.close()


_engine: SovereignSpatialEngine | None = None
_engine_lock = Lock()


def get_engine() -> SovereignSpatialEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = SovereignSpatialEngine()
        return _engine


class SpatialAPI(BaseHTTPRequestHandler):
    """Local-only HTTP for private viewers. Bind 127.0.0.1 by default."""

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[spatial-api] {self.address_string()} {fmt % args}")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        eng = get_engine()
        if path in ("/", "/health"):
            self._json(
                200, {"status": "ok", "engine": "sovereign-spatial", "vendor": "none"}
            )
            return
        if path in ("/api/frame", "/api/spatial"):
            self._json(200, eng.snapshot())
            return
        if path == "/api/agents":
            frame = eng.snapshot()
            self._json(200, frame.get("agents", []))
            return
        if path == "/api/metrics":
            frame = eng.snapshot()
            self._json(200, frame.get("metrics", {}))
            return
        self._json(404, {"error": "not found"})


def serve_api(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), SpatialAPI)
    t = Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"[spatial] API on http://{host}:{port} (local-only recommended)")
    return httpd


def ascii_hud(frame: dict[str, Any]) -> str:
    m = frame.get("metrics") or {}
    lines = [
        f"SOVEREIGN SPATIAL  tick={frame['tick']}  t={frame['sim_time']:.2f}s  "
        f"backend={frame['backend']}",
        f"corridor origin→dest  agents={len(frame['agents'])}  "
        f"pulses_active={m.get('pulses_active', len(frame.get('capital_pulses') or []))}  "
        f"spawned={m.get('pulses_spawned', 0)}  settled={m.get('pulses_settled', 0)}  "
        f"origin_wealth={m.get('origin_aggregate_wealth', 0)}",
        "-" * 78,
    ]
    for a in frame["agents"]:
        st = a.get("state") or ("ARRIVED" if a.get("has_migrated") else a.get("action"))
        lines.append(
            f"  {a['id']:20s} {st:10s} p={a['gravity_p']:.3f} "
            f"lat={a['lat']:.4f} lon={a['lon']:.4f} "
            f"$={a.get('base_wealth', 0):.0f} rem={a.get('remittance_count', 0)}"
        )
    for p in frame.get("capital_pulses") or []:
        lines.append(
            f"  $pulse {p['agent_id']} ${p['value']:.0f} progress={p['progress']:.2f}"
        )
    return "\n".join(lines)


def _write_results_files() -> Path | None:
    """Persist human-readable results after a run."""
    try:
        import importlib.util

        show_path = ROOT / "scripts" / "show_results.py"
        spec = importlib.util.spec_from_file_location("show_results", show_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {show_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        report = mod.build_report()
        md_path = STATE_DIR / "RESULTS.md"
        html_path = STATE_DIR / "results.html"
        txt_path = STATE_DIR / "RESULTS.txt"
        md_path.write_text(mod.format_md(report), encoding="utf-8")
        html_path.write_text(mod.format_html(report), encoding="utf-8")
        txt_path.write_text(mod.format_text(report), encoding="utf-8")
        return md_path
    except Exception as exc:
        print(f"[spatial] results write skipped: {exc}")
        return None


def _print_run_summary(eng: SovereignSpatialEngine, snap: dict[str, Any]) -> None:
    m = snap.get("metrics") or {}
    print()
    print("=" * 64)
    print("  RESULTS (also saved — see paths below)")
    print("=" * 64)
    origin_w = float(m.get("origin_aggregate_wealth") or 0)
    spawned = m.get("pulses_spawned", 0)
    settled_p = m.get("pulses_settled", 0)
    with eng._lock:
        n = len(eng.agents)
        n_settled = sum(1 for a in eng.agents.values() if a.state == "SETTLED")
        total_rem = sum(a.total_remitted for a in eng.agents.values())
    print(f"  Agents SETTLED:     {n_settled}/{n}")
    print(f"  Origin wealth:      ${origin_w:,.2f}  (money sent home to Zacatecas)")
    print(f"  Capital pulses:     {settled_p}/{spawned} settled")
    print(f"  Total remitted:     ${total_rem:,.2f}")
    print()
    print(f"  {'Name':18s} {'State':10s} {'Start':>8s} {'Sent home':>10s} {'Yield':>7s}")
    print("  " + "-" * 58)
    with eng._lock:
        for a in eng.agents.values():
            start = a.start_wealth or (a.base_wealth + a.total_remitted)
            yld = (a.total_remitted / start) if start else 0.0
            print(
                f"  {a.name[:18]:18s} {a.state:10s} "
                f"${start:7.0f} ${a.total_remitted:9.0f} {yld:6.1%}"
            )
    print()
    print("  Learning:")
    for line in get_memory().summary_lines():
        print(f"    {line}")
    path = _write_results_files()
    print()
    if path:
        print(f"  Open results anytime:")
        print(f"    cat state/RESULTS.txt")
        print(f"    cat state/RESULTS.md")
        print(f"    xdg-open state/results.html   # browser")
        print(f"    python scripts/show_results.py")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sovereign custom spatial engine (no third-party game runtime)"
    )
    parser.add_argument("--hz", type=float, default=10.0, help="Physics tick rate")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="Seconds to run (0 = forever)"
    )
    parser.add_argument("--no-api", action="store_true")
    parser.add_argument("--host", default=API_HOST)
    parser.add_argument("--port", type=int, default=API_PORT)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--force-migrate",
        action="store_true",
        help=(
            "Force agents into MIGRATING (burn-in without Ollama). "
            "If every agent is already at destination, auto-resets the corridor."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset all agents to origin STAY before run (full corridor burn-in)",
    )
    parser.add_argument(
        "--no-auto-reset",
        action="store_true",
        help="Disable auto-reset when --force-migrate finds agents already at dest",
    )
    parser.add_argument(
        "--reset-origin-wealth",
        action="store_true",
        help="With --reset, zero origin aggregate_wealth counters (fresh economy)",
    )
    parser.add_argument(
        "--until-settled",
        action="store_true",
        help="Exit when all agents are SETTLED and pulses finished (no idle tail)",
    )
    parser.add_argument(
        "--proto",
        action="store_true",
        help=(
            "Prototype mode: reset + clean origin economy + force-migrate + "
            "until-settled (clean one-shot demo)"
        ),
    )
    args = parser.parse_args()

    if args.proto:
        args.reset = True
        args.reset_origin_wealth = True
        args.force_migrate = True
        args.until_settled = True
        if not args.duration:
            args.duration = 0.0  # rely on until-settled

    eng = get_engine()
    # Burn-in ergonomics: --force-migrate with no agents left at origin almost
    # always means "run the full corridor again". Auto-reset so a bare
    # `--force-migrate` still shows MIGRATING transit.
    if args.force_migrate and not args.reset and not args.no_auto_reset:
        with eng._lock:
            at_origin = sum(1 for a in eng.agents.values() if a.state == "STAY")
            already_away = len(eng.agents) - at_origin
        if already_away > 0 and at_origin == 0:
            print(
                f"[spatial] all {already_away} agents already at destination "
                f"(graph has_migrated) — auto --reset for full corridor burn-in"
            )
            args.reset = True
            # Prototype-style clean economy when auto-resetting for demo
            if not args.reset_origin_wealth and args.until_settled:
                args.reset_origin_wealth = True

    if args.reset:
        eng.reset_for_corridor_run(
            restore_wealth=True,
            reset_origin_wealth=args.reset_origin_wealth,
            force_migrate=args.force_migrate,
        )
    elif args.force_migrate:
        with eng._lock:
            for a in eng.agents.values():
                if a.state == "STAY":
                    a.last_action = "MIGRATE"
                    a.last_confidence = 0.99
                    a.begin_migration()

    # Snapshot start wealth for learning after bootstrap/reset
    with eng._lock:
        for a in eng.agents.values():
            if a.start_wealth <= 0:
                a.start_wealth = a.base_wealth

    httpd = None if args.no_api else serve_api(args.host, args.port)
    dt = 1.0 / max(args.hz, 0.1)
    started = time.time()
    mem = get_memory()
    print(
        "[spatial] custom engine online — STAY→MIGRATING→REMITTING→SETTLED "
        f"(learn remit_frac={mem.remittance_fraction():.3f})"
    )
    for line in mem.summary_lines():
        print(f"[spatial] learn {line}")
    try:
        while True:
            frame = eng.step(dt)
            if not args.quiet and frame["tick"] % max(1, int(args.hz)) == 0:
                print(ascii_hud(frame))
            if args.until_settled and eng.all_terminal():
                print("[spatial] all agents SETTLED — stopping (--until-settled)")
                break
            if args.duration > 0 and (time.time() - started) >= args.duration:
                break
            time.sleep(dt)
    except KeyboardInterrupt:
        print("\n[spatial] shutdown")
    finally:
        if httpd is not None:
            httpd.shutdown()
        snap = eng.snapshot()
        _print_run_summary(eng, snap)
        eng.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
