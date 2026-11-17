"""app.py — Flask REST API for the Fault-to-Recovery grid simulator (BESCOM Theme 9).

Endpoints:
  POST /api/login            {email, password}
  GET  /api/network/state    full grid snapshot (also advances live telemetry)
  POST /api/simulate/fault   {scenario:"S1"|"S2"} or {edge_id:"..."}
  POST /api/simulate/restore apply recommended switching actions
  POST /api/simulate/reset   restore default healthy topology
  GET  /api/events           ?type=fault&q=...  filterable audit log

The app also serves the compiled frontend at GET / (same origin → no CORS issues).
CORS is enabled so the file:// frontend works too.

Run:  python app.py   →  http://localhost:5000
"""
import os
from flask import Flask, jsonify, request, send_from_directory

from flask_cors import CORS

import db
import engine

app = Flask(__name__)
CORS(app, origins="*")  # allow file:// frontend and cross-origin dev

# Frontend HTML lives next to app.py (download/bescom-grid.html)
FRONTEND_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_FILE = "bescom-grid.html"

# ---------------------------------------------------------------- boot
@app.before_request
def _ensure_booted():
    if not getattr(app, "_booted", False):
        db.init_db()
        engine.recompute()
        if db.telemetry_empty():
            engine.seed_telemetry()
        app._booted = True


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, FRONTEND_FILE)

@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if email and len(password) >= 3:
        return jsonify({"ok": True, "token": "demo." + str(abs(hash(email))),
                        "user": {"email": email, "name": "Grid Operator",
                                 "role": "Shift Engineer", "station": "BESCOM Circle 4"}})
    return jsonify({"ok": False, "error": "Invalid credentials. Use admin@bescom.sim / demo123."}), 401

@app.route("/api/network/state", methods=["GET"])
def network_state():
    engine.tick_telemetry()
    return jsonify(engine.build_state())

@app.route("/api/simulate/fault", methods=["POST"])
def simulate_fault():
    body = request.get_json(silent=True) or {}
    scenario = body.get("scenario")
    edge_id = body.get("edge_id")
    if not scenario and not edge_id:
        return jsonify({"error": "Provide 'scenario' (S1/S2) or 'edge_id'."}), 400
    state = engine.trigger_fault(scenario=scenario, edge_id=edge_id)
    return jsonify(state)

@app.route("/api/simulate/restore", methods=["POST"])
def simulate_restore():
    state = engine.apply_restoration()
    return jsonify(state)

@app.route("/api/simulate/reset", methods=["POST"])
def simulate_reset():
    state = engine.reset_grid()
    return jsonify(state)

@app.route("/api/events", methods=["GET"])
def events():
    type_filter = request.args.get("type", "all")
    q = request.args.get("q", "").strip().lower()
    evs = db.get_events(limit=500)
    out = []
    for e in evs:
        if type_filter != "all" and e["type"] != type_filter:
            continue
        if q and q not in (e["detail"] or "").lower() and q not in (e["location"] or "").lower() \
                and q not in (e["fault_id"] or "").lower():
            continue
        out.append(engine._ser_event(e))
    return jsonify(out)

@app.errorhandler(400)
@app.errorhandler(401)
def _err(e):
    return jsonify({"error": str(e.description) if hasattr(e, "description") else str(e)}), e.code


if __name__ == "__main__":
    db.init_db()
    if db.telemetry_empty():
        engine.seed_telemetry()
    engine.recompute()
    print("=" * 60)
    print(" Fault to Recovery — BESCOM Theme 9 backend")
    print(" API:      http://localhost:5000/api")
    print(" Frontend: http://localhost:5000/")
    print(" Login:    admin@bescom.sim / demo123")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
