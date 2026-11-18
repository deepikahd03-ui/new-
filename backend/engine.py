"""engine.py — graph-based intelligence for the Fault-to-Recovery simulator.

Uses networkx for topology + pathfinding. All mutations are persisted to SQLite
via db.py; build_state() assembles the full snapshot the frontend expects
(camelCase keys, identical shape to the original mock so the React components
work unchanged).
"""
import json
import random
import time

import networkx as nx

import db

META = {
    "name": "BESCOM Virtual Distribution Network",
    "region": "Bengaluru — Circle 4 (BESCOM)",
    "operator": "Control Room Operator",
    "scada": "SCADA-BESCOM-T9",
    "version": "1.0.0",
}

FEEDERS = ("F1", "F2", "F3")
SUBSTATIONS = ("SUB-A", "SUB-B")

# Seed telemetry lookup (id -> nominal telemetry) for re-energising restored lines.
SEED_EDGE_TELEMETRY = {
    row[0]: {"voltage_kv": row[8], "current_a": row[9], "load_pct": row[10], "power_mw": row[11]}
    for row in db.SEED_EDGES
}

# ---------------------------------------------------------------- scenarios
SCENARIOS = {
    "S1": {
        "edge": "E-SUBA-F2",
        "type": "Overcurrent Fault (3-phase short)",
        "severity": "critical",
        "desc": "Permanent line fault on 33kV incomer Substation Alpha → Feeder F2. Protective relay tripped CB-A2. Areas B & C lose supply.",
        "affected_desc": "Area B + Area C disconnected",
        "spike_area_d": False,
    },
    "S2": {
        "edge": "E-SUBA-F2",
        "type": "Overcurrent Fault + Feeder F3 Thermal Overload (Double-fault / Peak Load)",
        "severity": "critical",
        "desc": "Line fault on A→F2 during system peak. Feeder F3 simultaneously at thermal limit (Area D load spiked). Tie restoration infeasible — no feasible route.",
        "affected_desc": "Area B + Area C isolated; F3 at capacity",
        "spike_area_d": True,
        "area_d_load": 6.7,
    },
}

# ---------------------------------------------------------------- power flow
def _adjacency_for_power(edges):
    """Directed adjacency for energisation BFS: closed+non-faulted edges; ties
    are bidirectional only when closed."""
    adj = {}
    for e in edges:
        if e["status"] == "faulted":
            continue
        if e["switch_state"] != "closed":
            continue  # open tie or open sectionaliser blocks flow
        adj.setdefault(e["from_node"], []).append(e["to_node"])
        if e["is_tie"]:
            adj.setdefault(e["to_node"], []).append(e["from_node"])
    return adj

def compute_powered_set(edges=None, nodes=None):
    if edges is None:
        edges = db.get_edges()
    if nodes is None:
        nodes = db.get_nodes()
    adj = _adjacency_for_power(edges)
    powered = set(s["id"] for s in nodes if s["type"] == "substation")
    queue = list(powered)
    while queue:
        cur = queue.pop(0)
        for nxt in adj.get(cur, []):
            if nxt not in powered:
                powered.add(nxt)
                queue.append(nxt)
    return powered

def _derive_edge_status(e, powered):
    if e["status"] == "faulted":
        return "faulted"
    if e["status"] == "restoration":
        return "restoration"
    if e["is_tie"] and e["switch_state"] == "open":
        return "isolated"
    if e["to_node"] not in powered:
        return "isolated"
    if (e.get("load_pct") or 0) >= 78:
        return "warning"
    return "healthy"

def recompute():
    """Recompute energisation, edge display status, and persist. Single tx."""
    with db.lock():
        conn = db.get_conn()
        edges = [dict(r) for r in conn.execute("SELECT * FROM edges")]
        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes")]
        powered = compute_powered_set(edges, nodes)
        for n in nodes:
            p = 1 if n["id"] in powered else 0
            if n["powered"] != p:
                conn.execute("UPDATE nodes SET powered=? WHERE id=?", (p, n["id"]))
        for e in edges:
            ns = _derive_edge_status(e, powered)
            if e["status"] != ns:
                conn.execute("UPDATE edges SET status=? WHERE id=?", (ns, e["id"]))
                e["status"] = ns
            if ns == "isolated":  # non-faulted isolated lines carry no current
                if e["voltage_kv"] or e["current_a"] or e["load_pct"] or e["power_mw"]:
                    conn.execute(
                        "UPDATE edges SET voltage_kv=0,current_a=0,load_pct=0,power_mw=0 WHERE id=?",
                        (e["id"],))
        conn.commit()

# ---------------------------------------------------------------- graph pathfinding
def build_pathfinding_graph():
    """Undirected graph for restoration pathfinding. Treats normally-open
    tie-switches as available edges (they can be CLOSED). Excludes faulted
    edges and open non-tie switches."""
    G = nx.Graph()
    nodes = db.get_nodes()
    for n in nodes:
        G.add_node(n["id"])
    edge_lookup = {}
    for e in db.get_edges():
        if e["status"] == "faulted":
            continue
        usable = e["switch_state"] == "closed" or e["is_tie"]
        if not usable:
            continue
        a, b = e["from_node"], e["to_node"]
        G.add_edge(a, b)
        edge_lookup[(a, b)] = e
        edge_lookup[(b, a)] = e
    return G, edge_lookup

def feeder_current_load(feeder_id):
    """Sum of consumer load currently supplied by feeder_id through its own
    active tree (closed, non-faulted, non-tie edges)."""
    edges = db.get_edges()
    nodes = db.get_nodes()
    adj = {}
    for e in edges:
        if e["status"] == "faulted" or e["is_tie"]:
            continue
        if e["switch_state"] != "closed":
            continue
        adj.setdefault(e["from_node"], []).append(e["to_node"])
    seen = {feeder_id}
    q = [feeder_id]
    while q:
        c = q.pop(0)
        for t in adj.get(c, []):
            if t not in seen:
                seen.add(t)
                q.append(t)
    return round(sum(n["load_mw"] for n in nodes if n["id"] in seen and n["type"] == "consumer" and n["load_mw"]), 3)

# ---------------------------------------------------------------- localisation
def build_relay_sequence(fault, edge):
    """Deterministic protective relay trip timeline (ms)."""
    spike_i = edge.get("current_a") or 0
    # derive feeder + breaker from the faulted edge
    nodes = {n["id"]: n for n in db.get_nodes()}
    feeder = edge["to_node"] if nodes.get(edge["to_node"], {}).get("type") == "feeder" else None
    if feeder is None:
        # walk upstream to find a feeder
        for n in nodes.values():
            if n["type"] == "feeder" and n.get("feeder_of"):
                feeder = n["id"]
                break
    relay = "R-" + (feeder or "F?")
    sw = edge.get("switch") or ""
    breaker = "CB-" + (sw.split("-")[1] if "-" in sw else sw)
    return [
        {"t": 0, "label": "Fault inception", "detail": f"{fault['type']} on {fault['edge_name']}", "tag": "fault"},
        {"t": 8, "label": "Overcurrent pickup", "detail": f"Relay {relay} (51/50) — I≫Ipickup, {int(spike_i)} A vs 300 A threshold", "tag": "pickup"},
        {"t": 14, "label": "Relay trip command", "detail": f"{relay} issued TRIP to {breaker}", "tag": "trip"},
        {"t": 45, "label": f"{breaker} opened", "detail": f"Circuit breaker {breaker} interrupted fault current", "tag": "break"},
        {"t": 52, "label": "Fault isolated", "detail": f"Upstream intact. Downstream {fault['affected_desc']} de-energised.", "tag": "iso"},
    ]

def detect_and_localise_fault(edge_id, scenario_id=None):
    edge = db.get_edge(edge_id)
    if not edge:
        raise ValueError(f"Unknown edge: {edge_id}")
    sc = SCENARIOS.get(scenario_id, {}) if scenario_id else {}

    # fault telemetry: overcurrent spike then dead
    orig_current = edge["current_a"] or 0
    spike = int(orig_current * 14)
    db.update_edge(edge_id,
                   status="faulted",
                   voltage_kv=round((edge["voltage_kv"] or 0) * 0.22, 2),
                   current_a=spike,
                   load_pct=100,
                   power_mw=0)
    edge = db.get_edge(edge_id)

    fault_id = "FLT-" + str(len(db.get_faults()) + 1).zfill(3)
    fault = {
        "id": fault_id,
        "ts": int(time.time() * 1000),
        "edge_id": edge_id,
        "edge_name": edge["name"],
        "type": sc.get("type", "Overcurrent Fault"),
        "severity": sc.get("severity", "critical"),
        "desc": sc.get("desc", f"Line fault on {edge['name']}."),
        "affected_desc": sc.get("affected_desc", "Downstream areas de-energised"),
        "scenario_id": scenario_id or "",
    }
    db.add_fault(fault)

    # anomalies (clear previous)
    db.clear_anomalies()
    ts0 = fault["ts"]
    db.add_anomaly({"id": f"AN-{fault_id}-1", "ts": ts0, "fault_id": fault_id,
                    "metric": "Overcurrent", "value": f"{spike} A", "threshold": "300 A",
                    "location": edge["name"], "severity": "critical"})
    db.add_anomaly({"id": f"AN-{fault_id}-2", "ts": ts0 + 30, "fault_id": fault_id,
                    "metric": "Undervoltage", "value": f"{edge['voltage_kv']} kV", "threshold": "29.7 kV",
                    "location": edge["name"], "severity": "high"})

    # recompute to identify de-energised downstream
    recompute()

    db.add_event({"type": "fault", "subtype": fault["type"], "fault_id": fault_id,
                  "location": edge["name"], "severity": fault["severity"],
                  "outcome": "detected", "detail": fault["desc"]})
    relay_seq = build_relay_sequence(fault, edge)
    db.add_event({"type": "localisation", "fault_id": fault_id, "location": edge["name"],
                  "outcome": "localised",
                  "detail": f"{relay_seq[2]['detail']}. Fault segment {edge['name']} isolated."})

    db.set_state("active_fault_id", fault_id)
    db.set_state("status_phase", "localised")
    return fault

# ---------------------------------------------------------------- restoration route
def _analyze_path(path_nodes, edge_lookup, nodes_by_id):
    feeder = None
    feeder_index = None
    ties = []
    segments = []
    for i in range(len(path_nodes) - 1):
        a, b = path_nodes[i], path_nodes[i + 1]
        e = edge_lookup.get((a, b))
        if e:
            segments.append(e)
            if e["is_tie"] and e["switch_state"] == "open":
                ties.append(e)
        nd = nodes_by_id.get(b)
        if feeder is None and nd and nd["type"] == "feeder":
            feeder = b
            feeder_index = i
    return feeder, feeder_index, ties, segments

def find_restoration_route():
    """networkx pathfinding + capacity check. Returns a plan dict (feasible or not)."""
    nodes = db.get_nodes()
    nodes_by_id = {n["id"]: n for n in nodes}
    edges = db.get_edges()
    powered = compute_powered_set(edges, nodes)
    affected = [n for n in nodes if n["type"] == "consumer" and n["id"] not in powered]
    if not affected:
        return None

    G, edge_lookup = build_pathfinding_graph()

    area_paths = {}
    unreachable = []
    for area in affected:
        best = None
        for sub in SUBSTATIONS:
            if sub not in G or area["id"] not in G:
                continue
            try:
                path = nx.shortest_path(G, sub, area["id"])
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            feeder, fi, ties, segs = _analyze_path(path, edge_lookup, nodes_by_id)
            if feeder is None or not ties:
                # need a tie to be a restoration route; skip paths with no tie
                continue
            cand = {"sub": sub, "feeder": feeder, "feeder_index": fi, "ties": ties,
                    "segments": segs, "path": path, "len": len(path)}
            if best is None or len(path) < best["len"]:
                best = cand
        if best:
            area_paths[area["id"]] = (area, best)
        else:
            unreachable.append(area)

    active_fault_id = db.get_state("active_fault_id") or ""
    fault = db.get_fault(active_fault_id) if active_fault_id else None

    if unreachable or not area_paths:
        return {
            "faultId": active_fault_id,
            "strategy": "No feasible alternative route",
            "altFeeder": "—",
            "tieSwitch": "—",
            "capacityCheck": {"feeder": "—", "capacityMW": 0, "currentLoadMW": 0,
                              "addedLoadMW": 0, "projectedLoadMW": 0, "marginMW": 0, "result": "Fail"},
            "actions": [],
            "restoreTargets": [a["id"] for a in affected],
            "restoreLoadMW": round(sum(a["load_mw"] for a in affected), 2),
            "feasible": False,
            "pathEdgeIds": [],
            "reason": "No alternative supply path exists through any tie-switch to the isolated area(s).",
        }

    # group affected areas by backup feeder
    groups = {}
    for area_id, (area, p) in area_paths.items():
        g = groups.setdefault(p["feeder"], {"ties": p["ties"], "areas": [], "added": 0.0,
                                            "segments_by_area": {}, "sub": p["sub"]})
        g["areas"].append(area)
        g["added"] += area["load_mw"] or 0
        g["segments_by_area"][area_id] = p["segments"]
        g["feeder_index"] = p["feeder_index"]

    # pick the feeder covering the most load (single dominant plan)
    feeder_id = max(groups, key=lambda f: groups[f]["added"])
    g = groups[feeder_id]
    feeder_node = nodes_by_id[feeder_id]
    cap = feeder_node["capacity_mw"]
    current = feeder_current_load(feeder_id)
    added = round(g["added"], 2)
    projected = round(current + added, 2)
    margin = round(cap - projected, 2)
    feasible = projected <= cap
    tie = g["ties"][0] if g["ties"] else None
    tie_name = tie["switch"] if tie else "—"
    sub_name = nodes_by_id.get(g["sub"], {}).get("name", g["sub"])

    # path edge ids = segments from the feeder onward, across all areas in group
    path_ids = []
    for area_id, segs in g["segments_by_area"].items():
        fi = g["feeder_index"]
        for seg in segs[fi:]:
            if seg["id"] not in path_ids:
                path_ids.append(seg["id"])

    # faulted edge (for the OPEN-isolate action)
    faulted_edge = None
    if fault:
        faulted_edge = db.get_edge(fault["edge_id"])

    actions = []
    if faulted_edge and faulted_edge.get("switch"):
        actions.append({"id": "a1", "kind": "open", "target": faulted_edge["switch"],
                        "desc": f"Open sectionaliser {faulted_edge['switch']} to confirm fault isolation on {faulted_edge['name']}",
                        "reason": "Lock out faulted segment"})
    if tie:
        actions.append({"id": "a2", "kind": "close", "target": tie_name,
                        "desc": f"Close normally-open tie-switch {tie_name} ({feeder_id}↔{g['sub']})",
                        "reason": f"Backfeed {feeder_id} downstream from Feeder {feeder_id}"})

    if feasible:
        strategy = f"Tie-switch backfeed via Feeder {feeder_id}"
        reason = ""
    else:
        strategy = "No feasible alternative route"
        reason = (f"Backup Feeder {feeder_id} would exceed 100% thermal capacity "
                  f"(projected {projected:.2f} MW > {cap} MW cap, margin {margin:.2f} MW)")

    return {
        "faultId": active_fault_id,
        "strategy": strategy,
        "altFeeder": f"{feeder_id} ({sub_name})",
        "tieSwitch": tie_name,
        "capacityCheck": {"feeder": feeder_id, "capacityMW": cap, "currentLoadMW": current,
                          "addedLoadMW": added, "projectedLoadMW": projected,
                          "marginMW": margin, "result": "Pass" if feasible else "Fail"},
        "actions": actions,
        "restoreTargets": [a["id"] for a in g["areas"]],
        "restoreLoadMW": added,
        "feasible": feasible,
        "pathEdgeIds": path_ids if feasible else [],
        "reason": reason,
    }

# ---------------------------------------------------------------- restoration apply
def apply_restoration():
    plan = db.get_state_json("restoration_plan")
    if not plan:
        return build_state()
    if not plan.get("feasible"):
        db.set_state("status_phase", "no_route")
        db.add_event({"type": "restoration", "fault_id": plan.get("faultId", ""),
                      "outcome": "no_route",
                      "detail": "No feasible route — alternative feeder exceeds thermal capacity. Manual dispatch required."})
        recompute()
        return build_state()

    # execute switching actions
    edges = db.get_edges()
    for a in plan["actions"]:
        for e in edges:
            if e["switch"] == a["target"]:
                db.update_edge(e["id"], switch_state="closed" if a["kind"] == "close" else "open")

    # mark restoration path edges + re-energise telemetry from seed
    for eid in plan["pathEdgeIds"]:
        e = db.get_edge(eid)
        if e and e["status"] != "faulted":
            seed = SEED_EDGE_TELEMETRY.get(eid, {})
            db.update_edge(eid, status="restoration",
                           voltage_kv=seed.get("voltage_kv", e["voltage_kv"]),
                           current_a=seed.get("current_a", e["current_a"]),
                           load_pct=seed.get("load_pct", e["load_pct"]),
                           power_mw=seed.get("power_mw", e["power_mw"]))

    db.set_state("restoration_executed", "1")
    db.set_state_json("restoration_path", plan["pathEdgeIds"])
    db.set_state("status_phase", "restoring")
    recompute()
    db.set_state("status_phase", "restored")

    fault = db.get_fault(plan["faultId"]) if plan.get("faultId") else None
    dur = round((time.time() * 1000 - fault["ts"]) / 1000) if fault else 0
    sw_summary = "; ".join(f"{a['kind'].upper()} {a['target']}" for a in plan["actions"])
    db.add_event({"type": "switching", "fault_id": plan["faultId"], "outcome": "executed",
                  "detail": f"{sw_summary}. Backfeed active via {plan['altFeeder']}."})
    db.add_event({"type": "restoration", "fault_id": plan["faultId"], "outcome": "restored",
                  "duration_sec": dur,
                  "detail": f"Areas re-energised via {plan['tieSwitch']} backfeed from {plan['altFeeder']}. Restoration time {dur}s."})
    return build_state()

# ---------------------------------------------------------------- telemetry tick
def tick_telemetry():
    """Jitter powered line telemetry, log per-feeder samples, prune, recompute."""
    import random as _r
    with db.lock():
        conn = db.get_conn()
        edges = [dict(r) for r in conn.execute("SELECT * FROM edges")]
        for e in edges:
            if e["is_tie"]:
                continue
            if e["status"] not in ("healthy", "warning", "restoration"):
                continue
            v = e["voltage_kv"] or 0
            i = e["current_a"] or 0
            load = e["load_pct"] or 0
            v = round(v * (1 + (_r.random() - 0.5) * 0.012), 2)
            i = max(0, round(i + (_r.random() - 0.5) * i * 0.04))
            load = max(0, min(100, round(load + (_r.random() - 0.5) * 2.2)))
            p = round(i * v * 1.732 / 1000, 2)
            conn.execute("UPDATE edges SET voltage_kv=?,current_a=?,load_pct=?,power_mw=? WHERE id=?",
                         (v, i, load, p, e["id"]))
        conn.commit()
    # per-feeder telemetry log
    edges = db.get_edges()
    now = int(time.time() * 1000)
    for fid in FEEDERS:
        src = next((e for e in edges if e["to_node"] == fid), None)
        if src:
            db.add_telemetry_log(fid, {"ts": now, "v": src["voltage_kv"], "a": src["current_a"],
                                       "load": src["load_pct"], "p": src["power_mw"]})
    db.prune_telemetry()
    recompute()

def seed_telemetry(n=3):
    db.clear_telemetry()
    edges = db.get_edges()
    now = int(time.time() * 1000)
    for k in range(n):
        for fid in FEEDERS:
            src = next((e for e in edges if e["to_node"] == fid), None)
            if src:
                db.add_telemetry_log(fid, {"ts": now - (n - k) * 2600, "v": src["voltage_kv"],
                                           "a": src["current_a"], "load": src["load_pct"], "p": src["power_mw"]})

# ---------------------------------------------------------------- triggers
def trigger_fault(scenario=None, edge_id=None):
    # reset topology to healthy first (keep event history for audit)
    db.reset_topology_to_seed()
    seed_telemetry()
    if scenario == "S3" or (not scenario and not edge_id):
        db.add_event({"type": "reset", "outcome": "reset",
                      "detail": "Grid reset to healthy nominal state."})
        recompute()
        return build_state()

    # apply scenario pre-conditions
    if scenario == "S2":
        db.update_node("AREA-D", load_mw=SCENARIOS["S2"]["area_d_load"])
        db.update_edge("E-F3-DT104", load_pct=94, current_a=540, power_mw=6.7)
        db.update_edge("E-SUBB-F3", load_pct=92, current_a=540, power_mw=6.7)

    faulted = edge_id or SCENARIOS[scenario]["edge"]
    detect_and_localise_fault(faulted, scenario)

    plan = find_restoration_route()
    db.set_state_json("restoration_plan", plan)
    db.set_state("restoration_executed", "0")
    db.set_state_json("restoration_path", [])
    recompute()
    return build_state()

def reset_grid():
    db.reset_topology_to_seed()
    seed_telemetry()
    db.add_event({"type": "reset", "outcome": "reset",
                  "detail": "Grid reset to healthy nominal state by operator."})
    recompute()
    return build_state()

# ---------------------------------------------------------------- serialisers
def _ser_node(n, powered_set):
    return {
        "id": n["id"], "name": n["name"], "type": n["type"], "x": n["x"], "y": n["y"],
        "voltageKV": n["voltage_kv"], "capacityMVA": n["capacity_mva"],
        "capacityMW": n["capacity_mw"], "capacityKVA": n["capacity_kva"],
        "baseLoad": n["base_load"], "feederOf": n["feeder_of"],
        "consumers": n["consumers"], "loadMW": n["load_mw"], "tag": n["tag"],
        "powered": n["id"] in powered_set,
    }

def _ser_edge(e, powered_set):
    status = _derive_edge_status(e, powered_set)
    return {
        "id": e["id"], "from": e["from_node"], "to": e["to_node"], "name": e["name"],
        "switch": e["switch"], "isTie": bool(e["is_tie"]), "switchState": e["switch_state"],
        "status": status,
        "telemetry": {"voltageKV": e["voltage_kv"], "currentA": e["current_a"],
                      "loadPct": e["load_pct"], "powerMW": e["power_mw"]},
    }

def _ser_fault(f):
    return {"id": f["id"], "ts": f["ts"], "edgeId": f["edge_id"], "edgeName": f["edge_name"],
            "type": f["type"], "severity": f["severity"], "desc": f["desc"],
            "affectedDesc": f["affected_desc"], "scenarioId": f["scenario_id"]}

def _ser_anomaly(a):
    return {"id": a["id"], "ts": a["ts"], "faultId": a["fault_id"], "metric": a["metric"],
            "value": a["value"], "threshold": a["threshold"], "location": a["location"],
            "severity": a["severity"]}

def _ser_event(ev):
    return {"id": ev["id"], "ts": ev["ts"], "type": ev["type"], "subtype": ev["subtype"],
            "faultId": ev["fault_id"], "location": ev["location"], "severity": ev["severity"],
            "outcome": ev["outcome"], "detail": ev["detail"], "durationSec": ev["duration_sec"]}

# ---------------------------------------------------------------- full state
def _kpis(nodes, edges, powered):
    non_tie = [e for e in edges if not e["is_tie"]]
    healthy = [e for e in non_tie if _derive_edge_status(e, powered) == "healthy"]
    active_feeders = [n for n in nodes if n["type"] == "feeder" and n["id"] in powered]
    affected = [n for n in nodes if n["type"] == "consumer" and n["id"] not in powered]
    events = db.get_events()
    total_faults = sum(1 for e in events if e["type"] == "fault")
    restored = [e for e in events if e["outcome"] == "restored" and e["duration_sec"] is not None]
    avg = round(sum(e["duration_sec"] for e in restored) / len(restored)) if restored else 0
    return {
        "gridHealthPct": round(len(healthy) / len(non_tie) * 100) if non_tie else 100,
        "activeFeeders": len(active_feeders),
        "affectedConsumers": sum(n["consumers"] or 0 for n in affected),
        "avgRestorationSec": avg,
        "totalFaults": total_faults,
        "restoredFaults": len(restored),
    }

def build_state():
    nodes = db.get_nodes()
    edges = db.get_edges()
    powered = compute_powered_set(edges, nodes)

    s_nodes = [_ser_node(n, powered) for n in nodes]
    s_edges = [_ser_edge(e, powered) for e in edges]
    affected_areas = [{"id": n["id"], "name": n["name"], "consumers": n["consumers"],
                       "loadMW": n["load_mw"], "tag": n["tag"]}
                      for n in nodes if n["type"] == "consumer" and n["id"] not in powered]
    downstream = [n["id"] for n in nodes if n["type"] != "substation" and n["id"] not in powered]

    active_fault_id = db.get_state("active_fault_id") or None
    faults = [_ser_fault(f) for f in db.get_faults()]
    anomalies = [_ser_anomaly(a) for a in db.get_anomalies()]
    events = [_ser_event(e) for e in db.get_events()]

    relay_sequence = []
    if active_fault_id:
        fault = db.get_fault(active_fault_id)
        edge = db.get_edge(fault["edge_id"]) if fault else None
        if fault and edge:
            relay_sequence = build_relay_sequence(fault, edge)

    plan = db.get_state_json("restoration_plan")
    return {
        "meta": META,
        "nodes": s_nodes,
        "edges": s_edges,
        "faults": faults,
        "anomalies": anomalies,
        "events": events,
        "relaySequence": relay_sequence,
        "activeFaultId": active_fault_id,
        "restorationPlan": plan,
        "restorationExecuted": db.get_state("restoration_executed") == "1",
        "restorationPath": db.get_state_json("restoration_path", []) or [],
        "statusPhase": db.get_state("status_phase") or "healthy",
        "telemetryHistory": db.get_telemetry_history(),
        "affectedAreas": affected_areas,
        "downstream": downstream,
        "kpis": _kpis(nodes, edges, powered),
    }
