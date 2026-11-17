"""db.py — SQLite persistence for the Fault-to-Recovery grid simulator.

Schema:
  nodes          — substations, feeders, transformers, consumer areas
  edges          — distribution lines / tie-switches with telemetry + status
  faults         — fault records
  anomalies      — threshold-violation log
  event_history  — audit log
  telemetry_logs — rolling per-feeder telemetry samples (drives live charts)
  app_state      — key/value session state (active fault, phase, plan, ...)

A single shared connection + lock serializes access (the app is a single-operator
simulation, so this is simple and safe).
"""
import json
import sqlite3
import threading
import time

DB_PATH = "grid.db"

_lock = threading.RLock()
_conn = None

# ---------------------------------------------------------------- seed data
# Mirrors the frontend topology exactly so the visual map and the backend agree.
SEED_NODES = [
    # id, name, type, x, y, voltage_kv, capacity_mva, capacity_mw, capacity_kva, base_load, feeder_of, consumers, load_mw, tag
    ("SUB-A", "Substation Alpha", "substation",   90, 300, 33.0,   25, None, None, 9.2,  None,   None, None, None),
    ("SUB-B", "Substation Beta",  "substation",  910, 300, 33.0,   25, None, None, 6.4,  None,   None, None, None),
    ("F1",    "Feeder F1",        "feeder",      250, 150, 11.0, None,    8, None, None, "SUB-A", None, None, None),
    ("F2",    "Feeder F2",        "feeder",      250, 300, 11.0, None,    8, None, None, "SUB-A", None, None, None),
    ("F3",    "Feeder F3",        "feeder",      750, 470, 11.0, None,    8, None, None, "SUB-B", None, None, None),
    ("DT-101","DT-101",           "transformer",470, 150, 0.433,None, None, 1000, None, None,    None, None, None),
    ("DT-102","DT-102",           "transformer",470, 250, 0.433,None, None, 1500, None, None,    None, None, None),
    ("DT-103","DT-103",           "transformer",470, 350, 0.433,None, None, 1500, None, None,    None, None, None),
    ("DT-104","DT-104",           "transformer",600, 470, 0.433,None, None, 1250, None, None,    None, None, None),
    ("AREA-A","Area A",           "consumer",    720, 150, None, None, None, None, None, None,   1180, 1.78, "Residential"),
    ("AREA-B","Area B",           "consumer",    720, 250, None, None, None, None, None, None,   1860, 2.12, "Mixed Commercial"),
    ("AREA-C","Area C",           "consumer",    720, 350, None, None, None, None, None, None,   1640, 1.94, "Industrial Light"),
    ("AREA-D","Area D",           "consumer",    820, 470, None, None, None, None, None, None,   1420, 1.61, "Residential"),
]

# id, from_node, to_node, name, switch, is_tie, switch_state, status, voltage_kv, current_a, load_pct, power_mw
SEED_EDGES = [
    ("E-SUBA-F1",  "SUB-A", "F1",     "33kV Incomer A→F1",        "SW-A1", 0, "closed", "healthy", 33.0, 188, 46, 3.0),
    ("E-SUBA-F2",  "SUB-A", "F2",     "33kV Incomer A→F2",        "SW-A2", 0, "closed", "healthy", 33.0, 318, 71, 4.5),
    ("E-SUBB-F3",  "SUB-B", "F3",     "33kV Incomer B→F3",        "SW-B3", 0, "closed", "healthy", 33.0, 152, 38, 2.4),
    ("E-F1-DT101", "F1",    "DT-101", "11kV Lateral F1→DT-101",   "SW-1",  0, "closed", "healthy", 11.0, 142, 58, 1.78),
    ("E-F2-DT102", "F2",    "DT-102", "11kV Lateral F2→DT-102",   "SW-2",  0, "closed", "healthy", 11.0, 196, 73, 2.12),
    ("E-F2-DT103", "F2",    "DT-103", "11kV Lateral F2→DT-103",   "SW-3",  0, "closed", "healthy", 11.0, 178, 68, 1.94),
    ("E-F3-DT104", "F3",    "DT-104", "11kV Lateral F3→DT-104",   "SW-4",  0, "closed", "healthy", 11.0, 130, 52, 1.61),
    ("E-TS1",      "F2",    "F3",     "Tie-Line TS-1 (F2↔F3)",     "TS-1",  1, "open",   "isolated", 0.0, 0,   0, 0.0),
    ("E-DT101-A",  "DT-101","AREA-A", "LV Service DT-101→Area A", "SW-5",  0, "closed", "healthy", 0.433,410, 58, 1.78),
    ("E-DT102-B",  "DT-102","AREA-B", "LV Service DT-102→Area B", "SW-6",  0, "closed", "healthy", 0.433,560, 73, 2.12),
    ("E-DT103-C",  "DT-103","AREA-C", "LV Service DT-103→Area C", "SW-7",  0, "closed", "healthy", 0.433,512, 68, 1.94),
    ("E-DT104-D",  "DT-104","AREA-D", "LV Service DT-104→Area D", "SW-8",  0, "closed", "healthy", 0.433,380, 52, 1.61),
]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY,
        name TEXT, type TEXT, x REAL, y REAL,
        voltage_kv REAL, capacity_mva REAL, capacity_mw REAL, capacity_kva REAL,
        base_load REAL, feeder_of TEXT, consumers INTEGER, load_mw REAL, tag TEXT,
        powered INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS edges (
        id TEXT PRIMARY KEY,
        from_node TEXT, to_node TEXT, name TEXT,
        switch TEXT, is_tie INTEGER, switch_state TEXT, status TEXT,
        voltage_kv REAL, current_a REAL, load_pct REAL, power_mw REAL
    )""",
    """CREATE TABLE IF NOT EXISTS faults (
        id TEXT PRIMARY KEY, ts INTEGER, edge_id TEXT, edge_name TEXT,
        type TEXT, severity TEXT, desc TEXT, affected_desc TEXT, scenario_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS anomalies (
        id TEXT PRIMARY KEY, ts INTEGER, fault_id TEXT, metric TEXT,
        value TEXT, threshold TEXT, location TEXT, severity TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS event_history (
        id TEXT PRIMARY KEY, ts INTEGER, type TEXT, subtype TEXT,
        fault_id TEXT, location TEXT, severity TEXT, outcome TEXT,
        detail TEXT, duration_sec REAL
    )""",
    """CREATE TABLE IF NOT EXISTS telemetry_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, feeder_id TEXT,
        voltage_kv REAL, current_a REAL, load_pct REAL, power_mw REAL
    )""",
    """CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY, value TEXT
    )""",
]

DEFAULT_APP_STATE = {
    "active_fault_id": "",
    "status_phase": "healthy",
    "restoration_plan": "null",
    "restoration_executed": "0",
    "restoration_path": "[]",
}

# ---------------------------------------------------------------- connection
def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            c = sqlite3.connect(DB_PATH, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            _conn = c
        return _conn

def lock():
    return _lock

# ---------------------------------------------------------------- init / seed
def init_db():
    conn = get_conn()
    with _lock:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        # seed if empty
        n = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        if n == 0:
            for row in SEED_NODES:
                cur.execute(
                    """INSERT INTO nodes (id,name,type,x,y,voltage_kv,capacity_mva,
                       capacity_mw,capacity_kva,base_load,feeder_of,consumers,load_mw,tag,powered)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""", row)
            for row in SEED_EDGES:
                cur.execute(
                    """INSERT INTO edges (id,from_node,to_node,name,switch,is_tie,
                       switch_state,status,voltage_kv,current_a,load_pct,power_mw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", row)
            for k, v in DEFAULT_APP_STATE.items():
                cur.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)", (k, v))
            conn.commit()
        # ensure all app_state keys exist
        for k, v in DEFAULT_APP_STATE.items():
            cur.execute("INSERT OR IGNORE INTO app_state (key,value) VALUES (?,?)", (k, v))
        conn.commit()

# ---------------------------------------------------------------- node queries
def get_nodes():
    with _lock:
        return [dict(r) for r in get_conn().execute("SELECT * FROM nodes ORDER BY y, x").fetchall()]

def get_node(node_id):
    with _lock:
        r = get_conn().execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(r) if r else None

def update_node(node_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        get_conn().execute(f"UPDATE nodes SET {cols} WHERE id=?", (*fields.values(), node_id))
        get_conn().commit()

# ---------------------------------------------------------------- edge queries
def get_edges():
    with _lock:
        return [dict(r) for r in get_conn().execute("SELECT * FROM edges ORDER BY id").fetchall()]

def get_edge(edge_id):
    with _lock:
        r = get_conn().execute("SELECT * FROM edges WHERE id=?", (edge_id,)).fetchone()
        return dict(r) if r else None

def update_edge(edge_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        get_conn().execute(f"UPDATE edges SET {cols} WHERE id=?", (*fields.values(), edge_id))
        get_conn().commit()

# ---------------------------------------------------------------- fault queries
def get_faults():
    with _lock:
        return [dict(r) for r in get_conn().execute("SELECT * FROM faults ORDER BY ts DESC").fetchall()]

def get_fault(fault_id):
    with _lock:
        r = get_conn().execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
        return dict(r) if r else None

def add_fault(fault):
    with _lock:
        get_conn().execute(
            """INSERT INTO faults (id,ts,edge_id,edge_name,type,severity,desc,affected_desc,scenario_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (fault["id"], fault["ts"], fault["edge_id"], fault["edge_name"],
             fault["type"], fault["severity"], fault["desc"], fault["affected_desc"], fault["scenario_id"]))
        get_conn().commit()

# ---------------------------------------------------------------- anomaly queries
def get_anomalies():
    with _lock:
        return [dict(r) for r in get_conn().execute("SELECT * FROM anomalies ORDER BY ts DESC").fetchall()]

def add_anomaly(a):
    with _lock:
        get_conn().execute(
            """INSERT INTO anomalies (id,ts,fault_id,metric,value,threshold,location,severity)
               VALUES (?,?,?,?,?,?,?,?)""",
            (a["id"], a["ts"], a["fault_id"], a["metric"], a["value"], a["threshold"], a["location"], a["severity"]))
        get_conn().commit()

def clear_anomalies():
    with _lock:
        get_conn().execute("DELETE FROM anomalies")
        get_conn().commit()

# ---------------------------------------------------------------- event queries
def get_events(limit=200):
    with _lock:
        return [dict(r) for r in get_conn().execute(
            "SELECT * FROM event_history ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]

def add_event(ev):
    eid = ev.get("id") or ("EV-" + str(int(time.time() * 1000)) + "-" + str(abs(hash(ev.get("detail", ""))))[:5])
    with _lock:
        get_conn().execute(
            """INSERT OR REPLACE INTO event_history
               (id,ts,type,subtype,fault_id,location,severity,outcome,detail,duration_sec)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eid, ev.get("ts", int(time.time() * 1000)), ev.get("type"), ev.get("subtype"),
             ev.get("fault_id"), ev.get("location"), ev.get("severity"), ev.get("outcome"),
             ev.get("detail", ""), ev.get("duration_sec")))
        get_conn().commit()

# ---------------------------------------------------------------- telemetry logs
def add_telemetry_log(feeder_id, t):
    with _lock:
        get_conn().execute(
            """INSERT INTO telemetry_logs (ts,feeder_id,voltage_kv,current_a,load_pct,power_mw)
               VALUES (?,?,?,?,?,?)""",
            (t["ts"], feeder_id, t["v"], t["a"], t["load"], t["p"]))
        get_conn().commit()

def prune_telemetry(per_feeder=40):
    with _lock:
        conn = get_conn()
        for fid in ("F1", "F2", "F3"):
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM telemetry_logs WHERE feeder_id=? ORDER BY ts DESC", (fid,)).fetchmany(per_feeder)]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM telemetry_logs WHERE feeder_id=? AND id NOT IN ({placeholders})",
                             (fid, *ids))
            else:
                conn.execute("DELETE FROM telemetry_logs WHERE feeder_id=?", (fid,))
        conn.commit()

def get_telemetry_history(per_feeder=30):
    out = {}
    with _lock:
        for fid in ("F1", "F2", "F3"):
            rows = get_conn().execute(
                "SELECT ts,voltage_kv,current_a,load_pct,power_mw FROM telemetry_logs WHERE feeder_id=? ORDER BY ts ASC",
                (fid,)).fetchall()
            rows = rows[-per_feeder:]
            out[fid] = [{"t": r["ts"], "v": r["voltage_kv"], "a": r["current_a"],
                         "p": r["power_mw"], "load": r["load_pct"]} for r in rows]
    return out

def clear_telemetry():
    with _lock:
        get_conn().execute("DELETE FROM telemetry_logs")
        get_conn().commit()

def telemetry_empty():
    with _lock:
        return get_conn().execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0] == 0

# ---------------------------------------------------------------- app_state (kv)
def get_state(key, default=None):
    with _lock:
        r = get_conn().execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_state(key, value):
    with _lock:
        get_conn().execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)", (key, str(value)))
        get_conn().commit()

def get_state_json(key, default=None):
    v = get_state(key)
    if v in (None, "", "null"):
        return default
    try:
        return json.loads(v)
    except Exception:
        return default

def set_state_json(key, value):
    set_state(key, json.dumps(value))

# ---------------------------------------------------------------- reset helpers
def reset_topology_to_seed():
    """Wipe mutable tables and re-seed (keeps event_history for audit)."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM faults")
        conn.execute("DELETE FROM anomalies")
        conn.execute("DELETE FROM telemetry_logs")
        for row in SEED_NODES:
            conn.execute(
                """INSERT INTO nodes (id,name,type,x,y,voltage_kv,capacity_mva,
                   capacity_mw,capacity_kva,base_load,feeder_of,consumers,load_mw,tag,powered)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""", row)
        for row in SEED_EDGES:
            conn.execute(
                """INSERT INTO edges (id,from_node,to_node,name,switch,is_tie,
                   switch_state,status,voltage_kv,current_a,load_pct,power_mw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", row)
        for k, v in DEFAULT_APP_STATE.items():
            conn.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)", (k, v))
        conn.commit()
