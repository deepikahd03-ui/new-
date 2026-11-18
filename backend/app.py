"""app.py — Flask REST API for the Fault-to-Recovery grid simulator (BESCOM Theme 9).

Endpoints:
  POST /api/login            {email, password} -> sends OTP to email
  POST /api/auth/send-otp    {email}
  POST /api/auth/verify-otp  {email, otp} -> returns token & user profile
  POST /api/auth/resend-otp  {email} -> generates fresh OTP
  GET  /api/health           backend health check
  GET  /api/network/state    full grid snapshot (also advances live telemetry)
  POST /api/simulate/fault   {scenario:"S1"|"S2"} or {edge_id:"..."}
  POST /api/simulate/restore apply recommended switching actions
  POST /api/simulate/reset   restore default healthy topology
  GET  /api/events           ?type=fault&q=...  filterable audit log

Serves the compiled frontend at GET / (same origin → no CORS issues).
CORS is enabled so file:// and cross-origin frontends work as well.

Run:  python app.py   →  http://localhost:5000
"""
import os
import random
import time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import db
import engine
import mailer

app = Flask(__name__)
CORS(app, origins="*")  # allow file:// frontend and cross-origin dev

# Frontend HTML resolution (checks backend directory and parent directory)
BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
PARENT_DIR = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
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
    if os.path.exists(os.path.join(BACKEND_DIR, FRONTEND_FILE)):
        return send_from_directory(BACKEND_DIR, FRONTEND_FILE)
    return send_from_directory(PARENT_DIR, FRONTEND_FILE)

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "status": "online",
        "service": "BESCOM SCADA Grid Backend",
        "version": "1.0.0",
        "endpoints": [
            "/api/login",
            "/api/auth/send-otp",
            "/api/auth/verify-otp",
            "/api/auth/resend-otp",
            "/api/network/state",
            "/api/simulate/fault",
            "/api/simulate/restore",
            "/api/simulate/reset",
            "/api/events"
        ]
    })

@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or len(password) < 3:
        return jsonify({"ok": False, "error": "Invalid credentials. Use admin@bescom.sim / demo123."}), 401

    # Generate 6-digit OTP
    otp = f"{random.randint(100000, 999999)}"
    db.save_otp(email, otp, ttl_seconds=300)

    # Dispatch via email / terminal
    mail_res = mailer.send_otp_email(email, otp, ttl_minutes=5)

    return jsonify({
        "ok": True,
        "requires_otp": True,
        "email": email,
        "message": f"Verification passcode dispatched to {email}",
        "debug_otp": otp,  # Facilitates testing in prototype UI
        "method": mail_res.get("method", "simulated")
    })

@app.route("/api/auth/send-otp", methods=["POST"])
def send_otp():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "Email is required."}), 400
    
    otp = f"{random.randint(100000, 999999)}"
    db.save_otp(email, otp, ttl_seconds=300)
    mail_res = mailer.send_otp_email(email, otp, ttl_minutes=5)
    return jsonify({
        "ok": True,
        "email": email,
        "message": f"Verification code sent to {email}",
        "debug_otp": otp,
        "method": mail_res.get("method", "simulated")
    })

@app.route("/api/auth/verify-otp", methods=["POST"])
def verify_otp():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    otp = (body.get("otp") or "").strip()
    if not email or not otp:
        return jsonify({"ok": False, "error": "Email and OTP code are required."}), 400

    ok, msg = db.verify_otp(email, otp)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    token = f"bescom.sec.{abs(hash(email))}.{int(time.time())}"
    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "email": email,
            "name": "Grid Operator",
            "role": "Shift Engineer",
            "station": "BESCOM Circle 4"
        }
    })

@app.route("/api/auth/resend-otp", methods=["POST"])
def resend_otp():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "Email is required."}), 400

    otp = f"{random.randint(100000, 999999)}"
    db.save_otp(email, otp, ttl_seconds=300)
    mail_res = mailer.send_otp_email(email, otp, ttl_minutes=5)
    return jsonify({
        "ok": True,
        "message": f"New verification passcode sent to {email}",
        "debug_otp": otp,
        "method": mail_res.get("method", "simulated")
    })

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
        return jsonify({"error": "Provide 'scenario' (S1/S2/S3) or 'edge_id'."}), 400
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
