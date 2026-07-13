#!/usr/bin/env python3
"""
Inference Controller: Python + local Ollama API + pattern memory.

Polls agent state from the graph, asks the local LLM for a JSON decision
each simulation tick (with pattern-memory prior / offline fallback), and
publishes decisions for the custom sovereign spatial engine.

No third-party game engines. No cloud LLM required — Ollama optional; pattern memory always on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.graph_store import GraphBackend  # noqa: E402
from bridge.pattern_memory import get_memory  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
# Prefer models actually present on this host
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

# Sovereign unified LLM client v6.1
_LLM = None
try:
    _earth = Path.home() / "projects" / "sovereign-earth"
    if str(_earth) not in sys.path:
        sys.path.insert(0, str(_earth))
    from llm_client import generate as _llm_generate, resolve_model as _llm_resolve  # type: ignore
    _LLM = {"generate": _llm_generate, "resolve": _llm_resolve}
    DEFAULT_MODEL = _llm_resolve("demographic", preferred=DEFAULT_MODEL)
except Exception:
    _LLM = None

STATE_DIR = ROOT / "state"
DECISIONS_PATH = STATE_DIR / "tick_decisions.json"
API_HOST = os.environ.get("DEMOGRAPHIC_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("DEMOGRAPHIC_API_PORT", "8767"))

_state_lock = Lock()
_latest: dict[str, Any] = {
    "tick": 0,
    "backend": "local",
    "model": DEFAULT_MODEL,
    "agents": {},
    "updated_at": None,
}


def evaluate_agent_decision(
    agent_id: str,
    wealth: float,
    network_size: int,
    origin_jobs: float,
    dest_jobs: float,
    border_friction: float,
    risk_tolerance: float = 0.75,
    model: str = DEFAULT_MODEL,
    use_ollama: bool = True,
) -> dict[str, Any]:
    memory = get_memory()
    features = {
        "base_wealth": wealth,
        "network_size": network_size,
        "origin_jobs": origin_jobs,
        "dest_jobs": dest_jobs,
        "border_friction": border_friction,
    }
    # Pattern prior always available (evolves across runs)
    prior = memory.recommend_action(
        wealth=wealth,
        network_size=network_size,
        origin_jobs=origin_jobs,
        dest_jobs=dest_jobs,
        border_friction=border_friction,
        risk_tolerance=risk_tolerance,
    )

    agent_state = {
        "id": agent_id,
        "base_wealth_usd": wealth,
        "family_network_at_destination": network_size,
        "origin_job_availability": origin_jobs,
        "destination_job_availability": dest_jobs,
        "border_friction_index": border_friction,
        "pattern_prior": {
            "action": prior["action"],
            "confidence": prior["confidence"],
            "bucket": prior.get("pattern_bucket"),
            "gravity_p": prior.get("gravity_p"),
        },
    }

    prompt = f"""
You are the behavioral engine for a demographic simulation.
Evaluate this agent's state: {json.dumps(agent_state)}.
A local pattern-memory prior suggests action={prior['action']}
(confidence={prior['confidence']}, gravity_p={prior.get('gravity_p')}).
You may follow or override the prior based on push/pull economics.
Return ONLY a JSON object with two keys:
'action' (string: strictly "MIGRATE", "STAY", or "TRANSFER_CAPITAL")
'confidence' (float between 0.0 and 1.0).
""".strip()

    decision: dict[str, Any] | None = None
    if use_ollama:
        parsed = None
        if _LLM is not None:
            try:
                res = _LLM["generate"](prompt, role="demographic", model=model)
                if res.get("ok") and res.get("text"):
                    raw = res["text"]
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed = None
        if parsed is None:
            payload = {
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.25},
            }
            try:
                response = requests.post(OLLAMA_URL, json=payload, timeout=8)
                response.raise_for_status()
                body = response.json()
                raw = body.get("response", "{}")
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (requests.exceptions.RequestException, json.JSONDecodeError, TypeError, ValueError):
                parsed = None
        if parsed is not None:
            try:
                action = str(parsed.get("action", "STAY")).upper()
                if action not in ("MIGRATE", "STAY", "TRANSFER_CAPITAL"):
                    action = "STAY"
                confidence = float(parsed.get("confidence", 0.5))
                confidence = max(0.0, min(1.0, confidence))
                # Blend LLM with pattern prior so learning sticks
                if action == prior["action"]:
                    confidence = max(confidence, 0.5 * confidence + 0.5 * float(prior["confidence"]))
                else:
                    if confidence < 0.55:
                        action = prior["action"]
                        confidence = float(prior["confidence"])
                decision = {
                    "action": action,
                    "confidence": round(confidence, 4),
                    "source": "ollama+pattern",
                    "pattern_bucket": prior.get("pattern_bucket"),
                    "gravity_p": prior.get("gravity_p"),
                    "llm_version": "6.1",
                }
            except (TypeError, ValueError):
                decision = None

    if decision is None:
        decision = {
            "action": prior["action"],
            "confidence": prior["confidence"],
            "source": prior.get("source", "pattern_memory"),
            "pattern_bucket": prior.get("pattern_bucket"),
            "gravity_p": prior.get("gravity_p"),
            "scores": prior.get("scores"),
        }

    memory.observe_decision(
        agent_id=agent_id,
        action=str(decision["action"]),
        confidence=float(decision["confidence"]),
        features=features,
        source=str(decision.get("source")),
    )
    return decision


def run_tick(
    backend: GraphBackend,
    model: str,
    tick: int,
    use_ollama: bool = True,
    apply_side_effects: bool = False,
) -> dict[str, Any]:
    """
    Run one inference tick.

    apply_side_effects=False by default so spatial owns remittance pulses
    (avoids double-debit during corridor prototype).
    """
    agents = backend.list_persons()
    memory = get_memory()
    thr = memory.migrate_threshold()
    decisions: dict[str, Any] = {}
    for agent in agents:
        if agent.get("has_migrated"):
            decisions[agent["id"]] = {
                "action": "TRANSFER_CAPITAL",
                "confidence": 0.9,
                "source": "post_migration",
            }
            continue
        decision = evaluate_agent_decision(
            agent_id=agent["id"],
            wealth=float(agent.get("base_wealth", 1500)),
            network_size=int(agent.get("network_size", 0)),
            origin_jobs=float(agent.get("origin_jobs", 10)),
            dest_jobs=float(agent.get("dest_jobs", 50)),
            border_friction=float(agent.get("border_friction", 0.6)),
            risk_tolerance=float(agent.get("risk_tolerance", 0.75)),
            model=model,
            use_ollama=use_ollama,
        )
        decisions[agent["id"]] = decision

        if apply_side_effects:
            if decision["action"] == "TRANSFER_CAPITAL" and decision["confidence"] >= 0.5:
                frac = memory.remittance_fraction()
                amount = float(agent.get("base_wealth", 1500)) * frac
                backend.record_capital_transfer(agent["id"], amount)
            elif decision["action"] == "MIGRATE" and decision["confidence"] >= thr:
                backend.update_person(agent["id"], {"migration_intent": True})
        elif decision["action"] == "MIGRATE" and decision["confidence"] >= thr:
            backend.update_person(agent["id"], {"migration_intent": True})

    payload = {
        "tick": tick,
        "backend": backend.backend,
        "model": model,
        "learning": {
            "policy": memory.policy(),
            "weights": memory.weights(),
            "summary": memory.summary_lines(),
        },
        "agents": decisions,
        "agent_states": {
            a["id"]: {
                "id": a["id"],
                "name": a.get("name"),
                "base_wealth": a.get("base_wealth"),
                "risk_tolerance": a.get("risk_tolerance"),
                "network_size": a.get("network_size"),
                "dest_jobs": a.get("dest_jobs"),
                "origin_jobs": a.get("origin_jobs"),
                "border_friction": a.get("border_friction", 0.6),
                "has_migrated": a.get("has_migrated", False),
            }
            for a in agents
        },
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DECISIONS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with _state_lock:
        _latest.clear()
        _latest.update(payload)
    return payload


class DecisionAPI(BaseHTTPRequestHandler):
    """Minimal local HTTP surface for private viewers (no middleware)."""

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[api] {self.address_string()} {fmt % args}")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            self._json(200, {"status": "ok", "service": "demographic-inference"})
            return
        if path in ("/api/decisions", "/api/tick"):
            with _state_lock:
                self._json(200, dict(_latest))
            return
        if path == "/api/agents":
            with _state_lock:
                self._json(200, _latest.get("agent_states", {}))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        if path == "/api/agent/arrived":
            agent_id = body.get("agent_id")
            if not agent_id:
                self._json(400, {"error": "agent_id required"})
                return
            backend = GraphBackend()
            try:
                backend.update_person(agent_id, {"has_migrated": True, "migration_intent": False})
                amount = float(body.get("remittance", 0) or 0)
                if amount > 0:
                    backend.record_capital_transfer(agent_id, amount)
                self._json(200, {"ok": True, "agent_id": agent_id})
            finally:
                backend.close()
            return

        self._json(404, {"error": "not found"})


def serve_api(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), DecisionAPI)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"[inference] API listening on http://{host}:{port}")
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="Local agent decision daemon + pattern learning")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--interval", type=float, default=8.0, help="Seconds between ticks")
    parser.add_argument("--once", action="store_true", help="Single tick then exit")
    parser.add_argument("--no-api", action="store_true", help="Do not start HTTP API")
    parser.add_argument("--host", default=API_HOST)
    parser.add_argument("--port", type=int, default=API_PORT)
    parser.add_argument(
        "--no-ollama",
        action="store_true",
        help="Skip Ollama; pattern memory only (fully offline)",
    )
    parser.add_argument(
        "--apply-side-effects",
        action="store_true",
        help="Allow inference to debit capital (default: spatial owns remittances)",
    )
    args = parser.parse_args()

    backend = GraphBackend()
    mem = get_memory()
    print(
        f"[inference] graph backend={backend.backend} model={args.model} "
        f"ollama={'off' if args.no_ollama else 'on'}"
    )
    for line in mem.summary_lines():
        print(f"[inference] learn {line}")

    httpd = None
    if not args.no_api:
        httpd = serve_api(args.host, args.port)

    tick = 0
    try:
        while True:
            tick += 1
            payload = run_tick(
                backend,
                args.model,
                tick,
                use_ollama=not args.no_ollama,
                apply_side_effects=args.apply_side_effects,
            )
            print(
                f"[inference] tick={tick} agents={len(payload['agents'])} "
                f"sample={list(payload['agents'].items())[:2]}"
            )
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[inference] shutdown")
    finally:
        if httpd is not None:
            httpd.shutdown()
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
