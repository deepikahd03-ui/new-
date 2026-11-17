# Fault to Recovery — BESCOM Theme 9

Real-time smart-grid monitoring + self-healing simulation for the BESCOM
Circle 4 (Bengaluru) distribution network.

## Files

| File | Purpose |
| --- | --- |
| `bescom-grid.html` | Frontend — single-file React app (Tailwind + Babel via CDN). Talks to the Flask API. |
| `app.py`           | Flask REST API. Serves the frontend at `/` and exposes `/api/*`. |
| `db.py`            | SQLite persistence (nodes, edges, faults, anomalies, events, telemetry logs, app state). |
| `engine.py`        | Graph intelligence (networkx) — power-flow, fault localisation, restoration pathfinding. |
| `requirements.txt` | Python deps. |

## Run

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000/
# login: admin@bescom.sim / demo123  (any email + 3+ char password works)
```

The first run creates `grid.db` (SQLite) in the working directory.

## API

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/api/login`            | `{email, password}` | `{ok, token, user}` |
| GET  | `/api/network/state`     | —                   | full grid snapshot (also advances live telemetry) |
| POST | `/api/simulate/fault`    | `{scenario:"S1"\|"S2"}` or `{edge_id}` | new grid snapshot |
| POST | `/api/simulate/restore`  | —                   | grid snapshot after recommended switching |
| POST | `/api/simulate/reset`    | —                   | grid snapshot, healthy topology |
| GET  | `/api/events?type=...&q=...` | —               | filterable audit log |

## Scenarios

- **S1** — 3-phase short on the 33 kV incomer Substation Alpha → Feeder F2.
  Areas B & C lose supply; tie-switch TS-1 backfeed via F3 is feasible.
- **S2** — Same fault during system peak; Feeder F3 simultaneously at thermal
  limit (Area D load spiked). No feasible tie restoration — `no_route`.
- **S3** — Reset to healthy nominal topology.
