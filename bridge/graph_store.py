"""
Local graph store with Neo4j primary and JSON file fallback.

When Neo4j is unavailable the simulation still runs entirely offline
against state/local_graph.json — zero cloud, zero middleware SaaS.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOCAL_GRAPH_PATH = STATE_DIR / "local_graph.json"

DEFAULT_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
DEFAULT_USER = os.environ.get("NEO4J_USER", "neo4j")
DEFAULT_PASSWORD = os.environ.get("NEO4J_PASSWORD", "sovereign_local")


def _seed_graph() -> dict[str, Any]:
    return {
        "nodes": {
            "loc_mx_001": {
                "labels": ["Location"],
                "id": "loc_mx_001",
                "name": "Zacatecas_Municipality",
                "jurisdiction": "Mexico",
                "geo_lat": 22.7709,
                "geo_lon": -102.5832,
                "aggregate_wealth": 0.0,
                "remittance_inflow": 0.0,
                "remittance_events": 0,
            },
            "loc_us_001": {
                "labels": ["Location"],
                "id": "loc_us_001",
                "name": "El_Paso_Hub",
                "jurisdiction": "USA",
                "geo_lat": 31.7619,
                "geo_lon": -106.4850,
                "aggregate_wealth": 0.0,
                "remittance_inflow": 0.0,
                "remittance_events": 0,
            },
            "agent_8472": {
                "labels": ["Person"],
                "id": "agent_8472",
                "name": "Jose_Rojas",
                "base_wealth": 1500,
                "risk_tolerance": 0.85,
                "network_size": 4,
                "origin_jobs": 12,
                "dest_jobs": 85,
                "border_friction": 0.6,
                "has_migrated": False,
                "origin_location_id": "loc_mx_001",
            },
            "corp_992": {
                "labels": ["Entity"],
                "id": "corp_992",
                "name": "Rojas_Logistics_LLC",
                "sector": "Transport",
            },
        },
        "edges": [
            {
                "type": "ORIGINATES_FROM",
                "from": "agent_8472",
                "to": "loc_mx_001",
                "props": {},
            },
            {
                "type": "BENEFICIAL_OWNER",
                "from": "agent_8472",
                "to": "corp_992",
                "props": {"stake": 1.0, "year_established": 2024},
            },
            {
                "type": "OPERATES_IN",
                "from": "corp_992",
                "to": "loc_us_001",
                "props": {},
            },
            {
                "type": "TRANSFERRED_CAPITAL",
                "from": "agent_8472",
                "to": "loc_mx_001",
                "props": {
                    "amount": 25000,
                    "type": "Remittance",
                    "currency": "USD",
                    "year": 2024,
                },
            },
        ],
    }


class LocalGraphStore:
    """Thread-safe JSON graph used when Bolt is down."""

    def __init__(self, path: Path = LOCAL_GRAPH_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(_seed_graph())

    def _read(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    def merge_person(self, person: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            pid = person["id"]
            existing = data["nodes"].get(pid, {"labels": ["Person"], "id": pid})
            existing.update(person)
            if "Person" not in existing.get("labels", []):
                existing["labels"] = list(set(existing.get("labels", []) + ["Person"]))
            data["nodes"][pid] = existing
            self._write(data)

    def merge_edge(
        self,
        edge_type: str,
        from_id: str,
        to_id: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            data = self._read()
            props = props or {}
            for e in data["edges"]:
                if e["type"] == edge_type and e["from"] == from_id and e["to"] == to_id:
                    e["props"].update(props)
                    self._write(data)
                    return
            data["edges"].append(
                {"type": edge_type, "from": from_id, "to": to_id, "props": props}
            )
            self._write(data)

    def list_persons(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read()
            return [
                deepcopy(n)
                for n in data["nodes"].values()
                if "Person" in n.get("labels", [])
            ]

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            node = data["nodes"].get(node_id)
            return deepcopy(node) if node else None

    def update_person(self, person_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            if person_id not in data["nodes"]:
                return
            data["nodes"][person_id].update(fields)
            self._write(data)

    def update_node(self, node_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            if node_id not in data["nodes"]:
                return
            data["nodes"][node_id].update(fields)
            self._write(data)

    def credit_location(self, location_id: str, amount: float) -> dict[str, Any]:
        """Add remittance capital to an origin Location (permanent graph state)."""
        with self._lock:
            data = self._read()
            node = data["nodes"].get(location_id)
            if node is None:
                node = {
                    "labels": ["Location"],
                    "id": location_id,
                    "aggregate_wealth": 0.0,
                    "remittance_inflow": 0.0,
                    "remittance_events": 0,
                }
                data["nodes"][location_id] = node
            node["aggregate_wealth"] = float(node.get("aggregate_wealth") or 0) + amount
            node["remittance_inflow"] = float(node.get("remittance_inflow") or 0) + amount
            node["remittance_events"] = int(node.get("remittance_events") or 0) + 1
            self._write(data)
            return deepcopy(node)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._read())


class GraphBackend:
    """Neo4j if reachable, otherwise LocalGraphStore."""

    def __init__(
        self,
        uri: str = DEFAULT_URI,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASSWORD,
    ) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.local = LocalGraphStore()
        self.driver = None
        self.backend = "local"
        self._try_neo4j()

    def _try_neo4j(self) -> None:
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
            driver.verify_connectivity()
            self.driver = driver
            self.backend = "neo4j"
        except Exception:
            self.driver = None
            self.backend = "local"

    def close(self) -> None:
        if self.driver is not None:
            self.driver.close()
            self.driver = None

    def apply_cypher_file(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        statements = [
            s.strip()
            for s in text.split(";")
            if s.strip() and not s.strip().startswith("//")
        ]
        # Keep multi-line statements that contain // comments
        statements = []
        buf: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or not stripped:
                continue
            buf.append(line)
            if stripped.endswith(";"):
                statements.append("\n".join(buf).rstrip(";").strip())
                buf = []
        if buf:
            statements.append("\n".join(buf).strip())

        if self.backend == "neo4j" and self.driver is not None:
            applied = 0
            with self.driver.session() as session:
                for stmt in statements:
                    if not stmt:
                        continue
                    session.run(stmt)
                    applied += 1
            return {"backend": "neo4j", "statements": applied}

        # Local fallback: seed from built-in schema equivalent
        seed = _seed_graph()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOCAL_GRAPH_PATH.open("w", encoding="utf-8") as f:
            json.dump(seed, f, indent=2)
        return {"backend": "local", "statements": len(statements), "seeded": True}

    def upsert_persons(self, persons: list[dict[str, Any]]) -> int:
        count = 0
        if self.backend == "neo4j" and self.driver is not None:
            cypher = """
            UNWIND $rows AS row
            MERGE (p:Person {id: row.id})
            SET p.name = row.name,
                p.first_name = row.first_name,
                p.last_name = row.last_name,
                p.dob = row.dob,
                p.birth_location = row.birth_location,
                p.base_wealth = row.base_wealth,
                p.risk_tolerance = row.risk_tolerance,
                p.network_size = row.network_size,
                p.origin_jobs = row.origin_jobs,
                p.dest_jobs = row.dest_jobs,
                p.border_friction = coalesce(row.border_friction, 0.6),
                p.cluster_id = row.cluster_id,
                p.has_migrated = coalesce(row.has_migrated, false)
            """
            with self.driver.session() as session:
                session.run(cypher, rows=persons)
            count = len(persons)
        else:
            for p in persons:
                self.local.merge_person(p)
                count += 1
        return count

    def list_persons(self) -> list[dict[str, Any]]:
        if self.backend == "neo4j" and self.driver is not None:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (p:Person)
                    RETURN p.id AS id, p.name AS name,
                           p.base_wealth AS base_wealth,
                           p.risk_tolerance AS risk_tolerance,
                           p.network_size AS network_size,
                           p.origin_jobs AS origin_jobs,
                           p.dest_jobs AS dest_jobs,
                           p.border_friction AS border_friction,
                           coalesce(p.has_migrated, false) AS has_migrated
                    """
                )
                rows = []
                for r in result:
                    rows.append(
                        {
                            "id": r["id"],
                            "name": r["name"] or r["id"],
                            "base_wealth": float(r["base_wealth"] or 1500),
                            "risk_tolerance": float(r["risk_tolerance"] or 0.75),
                            "network_size": int(r["network_size"] or 0),
                            "origin_jobs": float(r["origin_jobs"] or 10),
                            "dest_jobs": float(r["dest_jobs"] or 50),
                            "border_friction": float(r["border_friction"] or 0.6),
                            "has_migrated": bool(r["has_migrated"]),
                        }
                    )
                return rows
        return self.local.list_persons()

    def update_person(self, person_id: str, fields: dict[str, Any]) -> None:
        if self.backend == "neo4j" and self.driver is not None:
            sets = ", ".join(f"p.{k} = ${k}" for k in fields)
            params = {"id": person_id, **fields}
            with self.driver.session() as session:
                session.run(f"MATCH (p:Person {{id: $id}}) SET {sets}", **params)
        else:
            self.local.update_person(person_id, fields)

    def get_location(self, location_id: str) -> dict[str, Any] | None:
        if self.backend == "neo4j" and self.driver is not None:
            with self.driver.session() as session:
                rec = session.run(
                    """
                    MATCH (o:Location {id: $id})
                    RETURN o.id AS id, o.name AS name,
                           coalesce(o.aggregate_wealth, 0) AS aggregate_wealth,
                           coalesce(o.remittance_inflow, 0) AS remittance_inflow,
                           coalesce(o.remittance_events, 0) AS remittance_events
                    """,
                    id=location_id,
                ).single()
                if not rec:
                    return None
                return {
                    "id": rec["id"],
                    "name": rec["name"],
                    "aggregate_wealth": float(rec["aggregate_wealth"]),
                    "remittance_inflow": float(rec["remittance_inflow"]),
                    "remittance_events": int(rec["remittance_events"]),
                }
        return self.local.get_node(location_id)

    def record_capital_transfer(
        self,
        agent_id: str,
        amount: float,
        year: int = 2026,
        origin_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Permanent remittance write-back:
          - CREATE TRANSFERRED_CAPITAL edge
          - debit Person.base_wealth
          - credit Location.aggregate_wealth / remittance_inflow
          - mark Person.has_migrated
        Returns post-update agent/origin wealth for the spatial HUD.
        """
        origin = origin_id or "loc_mx_001"
        result: dict[str, Any] = {
            "agent_id": agent_id,
            "amount": amount,
            "origin_id": origin,
            "agent_wealth": None,
            "origin_wealth": None,
        }

        if self.backend == "neo4j" and self.driver is not None:
            with self.driver.session() as session:
                rec = session.run(
                    """
                    MATCH (a:Person {id: $aid})
                    OPTIONAL MATCH (a)-[:ORIGINATES_FROM]->(linked:Location)
                    WITH a, coalesce(linked, null) AS linked
                    MERGE (o:Location {id: coalesce(linked.id, $origin)})
                    ON CREATE SET o.name = $origin,
                                  o.aggregate_wealth = 0,
                                  o.remittance_inflow = 0,
                                  o.remittance_events = 0
                    CREATE (a)-[:TRANSFERRED_CAPITAL {
                        amount: $amount, type: 'Remittance',
                        currency: 'USD', year: $year, ts: datetime()
                    }]->(o)
                    SET a.base_wealth = coalesce(a.base_wealth, 0) - $amount,
                        a.has_migrated = true,
                        a.total_remitted = coalesce(a.total_remitted, 0) + $amount,
                        o.aggregate_wealth = coalesce(o.aggregate_wealth, 0) + $amount,
                        o.remittance_inflow = coalesce(o.remittance_inflow, 0) + $amount,
                        o.remittance_events = coalesce(o.remittance_events, 0) + 1
                    RETURN a.base_wealth AS agent_wealth,
                           o.aggregate_wealth AS origin_wealth,
                           o.id AS origin_id
                    """,
                    aid=agent_id,
                    amount=amount,
                    year=year,
                    origin=origin,
                ).single()
                if rec:
                    result["agent_wealth"] = float(rec["agent_wealth"] or 0)
                    result["origin_wealth"] = float(rec["origin_wealth"] or 0)
                    result["origin_id"] = rec["origin_id"] or origin
            return result

        # Local JSON graph fallback — still permanent on disk
        person = self.local.get_node(agent_id) or {"id": agent_id, "base_wealth": 0}
        # Prefer ORIGINATES_FROM edge if present
        snap = self.local.snapshot()
        for e in snap.get("edges") or []:
            if e.get("type") == "ORIGINATES_FROM" and e.get("from") == agent_id:
                origin = e.get("to") or origin
                break

        wealth = max(0.0, float(person.get("base_wealth", 0)) - amount)
        total_remitted = float(person.get("total_remitted") or 0) + amount
        self.local.update_person(
            agent_id,
            {
                "base_wealth": wealth,
                "has_migrated": True,
                "total_remitted": total_remitted,
                "origin_location_id": origin,
            },
        )
        # Append-style edge (unique key includes amount+ts via separate edges)
        with self.local._lock:
            data = self.local._read()
            data["edges"].append(
                {
                    "type": "TRANSFERRED_CAPITAL",
                    "from": agent_id,
                    "to": origin,
                    "props": {
                        "amount": amount,
                        "type": "Remittance",
                        "currency": "USD",
                        "year": year,
                        "ts": time_iso(),
                    },
                }
            )
            self.local._write(data)

        loc = self.local.credit_location(origin, amount)
        result["agent_wealth"] = wealth
        result["origin_wealth"] = float(loc.get("aggregate_wealth") or 0)
        result["origin_id"] = origin
        return result


def time_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
