# ETHUSDT V22 Python Worker

This folder contains your **real** frozen trading engine
(`engine/live_code.py` — V22_LONG + SHORT_NO_FILTER + Mandatory ML)
packaged as a Docker container that you run on the **Hostinger VPS**
alongside the Node/TanStack Start app.

The Node app cannot run Python / pandas / xgboost /
sklearn. That is why this lives in its own container. The container computes
signals exactly the way your training pipeline does, then POSTs them to the
app `/api/public/engine/ingest.*` endpoints so they appear in the
dashboard.

## Layout

```
worker/
  engine/live_code.py     # your frozen engine, BASE_DIR patched to ENGINE_BASE_DIR
  runtime/                # the directory layout live_code expects, prefilled
    model files/
      *.joblib            # live bundle + LONG + SHORT models
      *.json              # configs + feature lists
      v22_live_engine_export/run-2026-06-19/   # decision engine + parity files
    eth_feature_shortlist_outputs/
  data/                   # bundled Tardis LSR CSVs (3y of 15m account ratios)
  main.py                 # entrypoint: imports live_code, attaches ingester
  ingester.py             # bridges append_csv_row -> app HTTP ingest
  requirements.txt
  Dockerfile
```

## Required environment

| Var                    | Example                                            | Purpose                        |
| ---------------------- | -------------------------------------------------- | ------------------------------ |
| `ENGINE_BASE_DIR`      | `/app/runtime`                                     | already set in the Dockerfile  |
| `APP_API_BASE`         | `https://YOUR_DOMAIN_OR_SERVER_IP`                 | where to POST signals          |
| `ENGINE_SERVICE_TOKEN` | matches the frontend `ENGINE_SERVICE_TOKEN` secret | bearer for ingest endpoints    |
| `ENGINE_USER_ID`       | your Supabase auth user UUID                       | which account owns the signals |

Optional: `INGEST_TIMEOUT` (default 10s).

> Backward compatibility: if `APP_API_BASE` is not set, the worker still
> falls back to the legacy `LOVABLE_API_BASE` variable.

## Local run

```bash
cd worker
docker build -t v22-engine .
docker run --rm \
  -e APP_API_BASE="https://YOUR_DOMAIN_OR_SERVER_IP" \
  -e ENGINE_SERVICE_TOKEN="$ENGINE_SERVICE_TOKEN" \
  -e ENGINE_USER_ID="<your-uuid>" \
  v22-engine
```

You should see the same boot logs as `live_code.py` locally
(`🟢 Live ETHUSDT 15m V22 NEW EXPORT ENGINE up`), followed by a bar every
15 minutes. Every signal/trade row also POSTs to the app; check the
dashboard's Live Signal Feed.

## Deploy on the Hostinger VPS

The worker runs as a Docker Compose service on the same VPS as the Node app.
See the repository-root `VPS_DEPLOYMENT.md` for the full architecture and
step-by-step install. In short:

1. Clone the repo to `/opt/trading-bot` on the VPS.
2. Upload the git-ignored model/data artifacts (see
   `ARTIFACT_UPLOAD_CHECKLIST.md`) into `worker/runtime` and `worker/data`.
3. Create `worker/.env` from `worker/.env.example` and set `APP_API_BASE`,
   `ENGINE_SERVICE_TOKEN`, and `ENGINE_USER_ID`.
4. From `worker/`, run `docker compose up -d`.

To rotate artifacts later: replace the files under
`worker/runtime/model files/` on the VPS and run `docker compose up -d --build`.

## Verifying it works

After the container has been running for at least one closed 15m bar:

- `docker logs` shows `[BAR mode] ...` lines from
  live_code's `log_bar_mode`.
- The dashboard's **Live Signal Feed** receives a new row per bar
  (`POST /api/public/engine/ingest.signal`).
- When the engine opens or closes a position, a row appears in
  **History** (`POST /api/public/engine/ingest.trade`).

## Important — what's bundled and what isn't

Bundled: the **frozen ML bundle, LONG model, SHORT model, feature configs,
V22 decision/long-candidate engine exports, feature shortlist, 3 years of
Tardis LSR** (15m grid, global + top accounts). These reproduce the exact
training-time decision surface.

Not bundled: nothing else needed for inference. `live_code.py` fetches
ETHUSDT klines, premium/funding, OI, ETHBTC context and aggregated trades
live from Binance REST every 15m boundary.

## Cost

The container is mostly idle between bars and runs on the same Hostinger VPS
as the Node app, so it adds no separate hosting cost.
