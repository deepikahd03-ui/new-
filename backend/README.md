# Fault to Recovery — BESCOM Theme 9

Real-time smart-grid monitoring + self-healing simulation with Two-Factor Authentication (2FA) for the BESCOM Circle 4 (Bengaluru) distribution network.

## Architecture

| Component | Technology | Description |
| --- | --- | --- |
| `bescom-grid.html` | React 18 + Tailwind CSS + Lucide Icons | Single-file SCADA frontend with Login & OTP Email Verification pages, live SVG single-line diagrams, telemetry charts, and fault simulation controls. |
| `app.py` | Python 3 + Flask + Flask-CORS | REST API serving `/api/*` and compiled frontend at `/`. |
| `mailer.py` | Python `smtplib` + MIME | Email dispatcher for OTP passcodes (supports both real SMTP configuration and prototype console/preview simulation). |
| `db.py` | SQLite3 (`grid.db`) | Relational persistence for nodes, edges, telemetry logs, audit events, and OTP verification codes. |
| `engine.py` | NetworkX | Power-flow energisation BFS, automated fault localisation, and capacity-checked restoration pathfinding. |

---

## Authentication Flow (2-Step 2FA)

1. **Step 1 — Credentials**:
   - Operator enters registered email (e.g. `admin@bescom.sim`) and password (`demo123`).
   - Clicks **"Enter Control Room"**.
   - Backend validates credentials, generates a cryptographically secure 6-digit OTP, stores it in SQLite with 5-minute expiry, and dispatches it to the email.
2. **Step 2 — OTP Verification**:
   - The UI transitions into the **Two-Factor Authentication** page.
   - Operator inputs the 6-digit code received via email (with auto-focus traversal, backspace handling, paste support, and prototype one-click auto-fill).
   - Once verified, the operator is authenticated and seamlessly transitioned into the **SCADA Grid Dashboard**.

---

## How to Run

```bash
# 1. Activate environment or install dependencies
cd backend
pip install -r requirements.txt

# 2. Start the Flask backend
python app.py
```

- **Web Dashboard**: `http://localhost:5000/`
- **Default Credentials**: `admin@bescom.sim` / `demo123`
- **OTP Code**: Displayed on terminal console upon dispatch, and also surfaced via a prototype banner for instant one-click testing.

---

## REST API Endpoints

| Method | Path | Body / Query | Description |
| --- | --- | --- | --- |
| `POST` | `/api/login` | `{"email", "password"}` | Validates credentials & triggers 6-digit OTP email dispatch |
| `POST` | `/api/auth/send-otp` | `{"email"}` | Dispatches a new OTP to the specified email |
| `POST` | `/api/auth/verify-otp` | `{"email", "otp"}` | Validates 6-digit OTP code & returns session token + user profile |
| `POST` | `/api/auth/resend-otp` | `{"email"}` | Resends fresh 6-digit OTP with cooldown enforcement |
| `GET` | `/api/health` | — | Backend connectivity check |
| `GET` | `/api/network/state` | — | Advances live telemetry jitter and returns complete network topology snapshot |
| `POST` | `/api/simulate/fault` | `{"scenario": "S1"\|"S2"}` | Triggers fault scenario & protective relay sequence |
| `POST` | `/api/simulate/restore` | — | Applies recommended tie-switch backfeed switching actions |
| `POST` | `/api/simulate/reset` | — | Restores grid to nominal healthy state |
| `GET` | `/api/events` | `?type=...&q=...` | Query and filter historical audit events |

---

## Scenarios

- **S1** — 3-phase short on the 33 kV incomer Substation Alpha → Feeder F2. Areas B & C lose supply; tie-switch TS-1 backfeed via F3 is feasible.
- **S2** — Same fault during system peak; Feeder F3 simultaneously at thermal limit (Area D load spiked). No feasible tie restoration — `no_route`.
- **S3** — Reset to healthy nominal topology.
