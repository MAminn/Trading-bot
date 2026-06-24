# Hostinger VPS Deployment

This project is deployed on a **Hostinger VPS**. This document describes the
final architecture, the required environment variables, and the install
outline. It does **not** change any trading, ML, or Binance logic — it only
documents how the existing pieces are hosted.

## Architecture

```
                          Internet
                             |
                        ┌────▼────┐
                        │  Nginx  │  reverse proxy (TLS, :80/:443)
                        └────┬────┘
                             │ proxies to 127.0.0.1:3000
                  ┌──────────▼───────────┐
                  │  Node / TanStack     │  frontend + API
                  │  Start app           │  (PM2 or systemd)
                  └──────────┬───────────┘
                             │ reads/writes
                  ┌──────────▼───────────┐
                  │  Supabase (external) │  database + auth
                  └──────────▲───────────┘
                             │ frontend reads tables
                             │
                  ┌──────────┴───────────┐
                  │  Python Docker worker│  worker/ (docker compose)
                  │  V22 engine          │
                  └──────────┬───────────┘
                             │ POST signals/trades
                             ▼
        APP_API_BASE/api/public/engine/ingest.signal
        APP_API_BASE/api/public/engine/ingest.trade
```

Components:

- **Nginx reverse proxy** — terminates TLS and forwards public traffic to the
  Node app listening on `127.0.0.1:3000`.
- **Node / TanStack Start app** — serves the frontend UI and the
  `/api/...` routes (including the `/api/public/engine/ingest.*` ingest
  endpoints). Run under PM2 or systemd so it restarts on reboot/crash.
- **Python Docker worker** (`worker/`) — runs the frozen V22 engine in a
  container via Docker Compose. It computes signals every 15m bar and POSTs
  each new signal/trade row to `APP_API_BASE/api/public/engine/ingest.*`.
- **External Supabase** — remains the database and auth provider. We continue
  to use the hosted Supabase project unless/until the DB layer is rewritten.

Data flow:

- The worker posts to `APP_API_BASE/api/public/engine/ingest/signal` and
  `.../ingest/trade` (the app's public engine ingest endpoints), authenticated
  with `ENGINE_SERVICE_TOKEN`.
- The Node app writes the ingested rows into Supabase tables.
- The frontend reads from Supabase tables to render the dashboard.
- Model and data artifacts are **git-ignored** and therefore must be uploaded
  manually to the VPS after cloning (see `ARTIFACT_UPLOAD_CHECKLIST.md`).

## Required VPS environment variables

### Frontend / Node app

```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_PUBLISHABLE_KEY=
VITE_SUPABASE_URL=
VITE_SUPABASE_PUBLISHABLE_KEY=
ENGINE_SERVICE_TOKEN=
BINANCE_KEY_ENCRYPTION_SECRET=
```

### Worker

```
ENGINE_BASE_DIR=/app/runtime
APP_API_BASE=https://YOUR_DOMAIN_OR_SERVER_IP
ENGINE_SERVICE_TOKEN=same_value_as_frontend
ENGINE_USER_ID=your_supabase_user_uuid
```

> `ENGINE_SERVICE_TOKEN` must be **identical** in the frontend and the worker —
> it is the bearer token the worker uses to authenticate against the app's
> ingest endpoints.
>
> Do not commit real values. Keep them in untracked `.env` files on the VPS
> (`.env` for the Node app, `worker/.env` for the worker).

## Install outline

1. **Install Node** (LTS), e.g. via `nvm` or your distro package.
2. **Install Docker and Docker Compose** (Docker Engine + the
   `docker compose` plugin).
3. **Install Nginx**.
4. **Clone the repo** to `/opt/trading-bot`:
   ```bash
   sudo git clone <your-repo-url> /opt/trading-bot
   cd /opt/trading-bot
   ```
5. **Install dependencies**:
   ```bash
   npm install
   ```
6. **Build the app**:
   ```bash
   npm run build
   ```
7. **Run the frontend** under a process manager (PM2 or systemd) so it
   listens on `127.0.0.1:3000` and restarts automatically. Provide the
   frontend env vars listed above.
8. **Upload git-ignored artifacts** into `worker/runtime` and `worker/data`
   (see `ARTIFACT_UPLOAD_CHECKLIST.md`).
9. **Start the worker** from `worker/`:
   ```bash
   cd worker
   cp .env.example .env   # then edit .env with real values
   docker compose up -d
   ```
10. **Configure Nginx** as a reverse proxy to the Node app:

    ```nginx
    server {
        listen 80;
        server_name YOUR_DOMAIN_OR_SERVER_IP;

        location / {
            proxy_pass http://127.0.0.1:3000;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
    ```

    Reload Nginx, then add TLS (e.g. with Certbot) for HTTPS.

## Notes

- The old Lovable demo cron (`engine-builtin-tick`) has been disabled — on the
  VPS the Python worker drives the engine directly, so no external cron is
  scheduled.
- Trading logic, ML logic, thresholds, features, Binance logic, and model
  loading are unchanged. This is deployment cleanup and documentation only.
