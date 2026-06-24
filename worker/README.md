# ETHUSDT V22 Python Worker

This folder contains your **real** frozen trading engine
(`engine/live_code.py` — V22_LONG + SHORT_NO_FILTER + Mandatory ML)
packaged as a Docker container that you run **outside** Lovable.

Lovable Cloud (Cloudflare Workers) cannot run Python / pandas / xgboost /
sklearn. That is why this lives in its own container. The container computes
signals exactly the way your training pipeline does, then POSTs them to the
Lovable `/api/public/engine/ingest.*` endpoints so they appear in the
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
  ingester.py             # bridges append_csv_row -> Lovable HTTP ingest
  requirements.txt
  Dockerfile
```

## Required environment

| Var | Example | Purpose |
|---|---|---|
| `ENGINE_BASE_DIR` | `/app/runtime` | already set in the Dockerfile |
| `LOVABLE_API_BASE` | `https://project--e6e62973-7761-48f9-a0d1-e6b08bd22dff.lovable.app` | where to POST signals |
| `ENGINE_SERVICE_TOKEN` | matches the Lovable `ENGINE_SERVICE_TOKEN` secret | bearer for ingest endpoints |
| `ENGINE_USER_ID` | your Supabase auth user UUID | which account owns the signals |

Optional: `INGEST_TIMEOUT` (default 10s).

## Local run

```bash
cd worker
docker build -t v22-engine .
docker run --rm \
  -e LOVABLE_API_BASE="https://project--e6e62973-7761-48f9-a0d1-e6b08bd22dff.lovable.app" \
  -e ENGINE_SERVICE_TOKEN="$ENGINE_SERVICE_TOKEN" \
  -e ENGINE_USER_ID="<your-uuid>" \
  v22-engine
```

You should see the same boot logs as `live_code.py` locally
(`🟢 Live ETHUSDT 15m V22 NEW EXPORT ENGINE up`), followed by a bar every
15 minutes. Every signal/trade row also POSTs to Lovable; check the
dashboard's Live Signal Feed.

## Deploy to Render (recommended, easiest)

1. Push this repo to GitHub.
2. Render → **New +** → **Background Worker**.
3. Repository = this repo, Root Directory = `worker`, Runtime = **Docker**.
4. Environment variables: add the four vars above.
5. Plan: Starter ($7/mo) is enough — engine is idle ~99% of each 15m.
6. Click **Create Background Worker**. First build ≈ 5 min.

To rotate artifacts later: replace files under `worker/runtime/model files/`,
push, Render rebuilds automatically.

## Deploy to Fly.io (alternative)

```bash
cd worker
fly launch --no-deploy --copy-config --name v22-engine
fly secrets set \
  LOVABLE_API_BASE="https://project--e6e62973-7761-48f9-a0d1-e6b08bd22dff.lovable.app" \
  ENGINE_SERVICE_TOKEN="$ENGINE_SERVICE_TOKEN" \
  ENGINE_USER_ID="<your-uuid>"
fly deploy
```

## Verifying it works

After the container has been running for at least one closed 15m bar:

- `docker logs` (or Render/Fly logs) shows `[BAR mode] ...` lines from
  live_code's `log_bar_mode`.
- Lovable dashboard's **Live Signal Feed** receives a new row per bar
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

≈ $5–7 / month on Render Starter or Fly.io. The container is mostly idle
between bars.
