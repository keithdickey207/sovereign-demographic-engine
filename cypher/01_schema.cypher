// Establish primary keys and indexing for high-speed ingestion
CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT location_id IF NOT EXISTS FOR (l:Location) REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;

// Create the core nodes
MERGE (loc_origin:Location {id: "loc_mx_001", name: "Zacatecas_Municipality", jurisdiction: "Mexico", geo_lat: 22.7709, geo_lon: -102.5832})
SET loc_origin.aggregate_wealth = coalesce(loc_origin.aggregate_wealth, 0),
    loc_origin.remittance_inflow = coalesce(loc_origin.remittance_inflow, 0),
    loc_origin.remittance_events = coalesce(loc_origin.remittance_events, 0)
MERGE (loc_dest:Location {id: "loc_us_001", name: "El_Paso_Hub", jurisdiction: "USA", geo_lat: 31.7619, geo_lon: -106.4850})
SET loc_dest.aggregate_wealth = coalesce(loc_dest.aggregate_wealth, 0),
    loc_dest.remittance_inflow = coalesce(loc_dest.remittance_inflow, 0),
    loc_dest.remittance_events = coalesce(loc_dest.remittance_events, 0)
MERGE (agent:Person {id: "agent_8472", name: "Jose_Rojas", base_wealth: 1500, risk_tolerance: 0.85})
SET agent.total_remitted = coalesce(agent.total_remitted, 0)
MERGE (corp:Entity {id: "corp_992", name: "Rojas_Logistics_LLC", sector: "Transport"})

// Establish the relationships (Edges)
MERGE (agent)-[:ORIGINATES_FROM]->(loc_origin)
MERGE (agent)-[:BENEFICIAL_OWNER {stake: 1.0, year_established: 2024}]->(corp)
MERGE (corp)-[:OPERATES_IN]->(loc_dest)
MERGE (agent)-[:TRANSFERRED_CAPITAL {amount: 25000, type: "Remittance", currency: "USD", year: 2024}]->(loc_origin);
