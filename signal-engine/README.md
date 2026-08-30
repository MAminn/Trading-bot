# ETHUSDT Signal Engine (canonical)

The model owner's delivered final engine, packaged for production. This
replaces the previous-generation worker in `worker/`, which stays on disk as
rollback/reference and **must never run at the same time as this service**.

## Architecture

```
THIS SERVICE  (signal/model engine)
      |  POST /api/public/engine/ingest/{signal,trade}, /heartbeat
      v
existing website/backend signal API
      |
      v
existing separate multi-tenant executor   (executor/)
      |
      v
customer Binance USD-M Futures accounts
```

This service is the first box only. It **does not** hold customer Binance
credentials, size customer positions, set allocation or leverage, or place
Binance orders. The executor remains solely responsible for all of that, and
nothing in `executor/` changes because of this engine.

The engine is paper/shadow by construction and enforces it at startup:
`_startup_no_order_scan` fails the boot if any order endpoint or mutating
`requests.*` call appears in the source of any function in the engine module.
Signal emission therefore lives in `ingester.py`, outside the engine package.

## What is frozen

`engine/live_code.py` is the delivered engine copied byte-for-byte, with
**exactly two** infrastructure adaptations (see `engine/__init__.py`):
`BASE_DIR` from `ENGINE_BASE_DIR`, and emptied email credential defaults.

Not modified, and not to be modified without a separate decision: model
features, thresholds, candidate logic, RealAgg logic, OI logic, HTF logic, ML
inference, entry/exit logic, the global no-overlap policy, LONG_FIRST, and the
no-flip policy.

The engine self-verifies this. At startup it hashes the source of 13 critical
functions against `EXPECTED_CRITICAL_LOGIC_SHA256` and refuses to start on any
mismatch — including whitespace. It also hashes all 10 model artifacts and
asserts the live contract below.

## Product contract (asserted at startup, from the bundle/config)

| | |
|---|---|
| Symbol / base timeframe | ETHUSDT / 15m |
| LONG threshold | 0.49 |
| SHORT threshold | 0.44 |
| LONG / SHORT ML features | 120 / 120 |
| Historical final portfolio | 2922 trades (LONG 1001, SHORT 1921) |
| Overlap | global no-overlap |
| Same-bar priority | LONG_FIRST |
| Flip | no flip while a position is open |
| Entry | NEXT 15m open |

> The `v22_live_engine_export/` files carry `final_trades 3626`,
> `long_threshold 0.40`, `short_threshold 0.39`. Those are **stale export
> metadata and are logged as metadata only** — the engine says so in its own
> `[EXPORT ENGINE CHECK]` log line. They are not the operating thresholds. The
> export directory is still required: its `v22_live_engine_candidate_audit.csv`
> supplies the 1939 historical LONG candidate timestamps, and startup asserts
> that count.

## Market data

ETHUSDT base OHLCV, premium/funding, RealAgg and Open Interest all come from
Binance **USD-M Futures**; BTCUSDT context is USD-M Futures; ETHBTC context is
**Spot**. RealAgg is true `aggTrades` aggregated to 1m (not a kline
approximation) and Open Interest is true 5m snapshots — both are live-data
fixes over the previous generation and are contract-checked at startup by
`_startup_external_source_static_contract`. Do not substitute approximations.

## Production filesystem contract

Two host directories, two container mounts. Nothing below is committed to Git or
baked into the image — see "Staging artifacts".

| Host | Container | Holds |
|---|---|---|
| `/opt/trading-bot/signal-engine/runtime` | `/app/runtime` (`ENGINE_BASE_DIR`) | artifacts + engine-written data |
| `/opt/trading-bot/signal-engine/state` | `/app/outbox` | durable integration state |

Both are **directory** mounts. `/app/outbox` must never be a single-file bind:
SQLite runs in WAL mode and needs `outbox.db`, `outbox.db-wal` and
`outbox.db-shm` side by side.

**Read-only input artifacts** — staged once, then only rotated deliberately:

- the four historical enriched CSVs, `ETHUSDT_{15m,1h,4h,1d}_BINANCE_*_clean_raw_plus_external.csv`
- the feature shortlist, `eth_feature_shortlist_outputs/ethusdt_feature_shortlist_best3_global.csv`
- the 10 final model artifacts, in `model files/`
- the four V22 export files, in `model files/v22_live_engine_export/run_*/`

**Writable / persistent engine data** — inside the same `runtime` mount:

- `model files/runtime_cache/` — the RealAgg 1m cache. Without persistence the
  engine re-downloads four days of USD-M aggTrades daily zips on every restart.
- `model files/shadow_live_v22_candidate_match_audit/` — the five audit files,
  including `shadow_live_state.json`, whose `last_processed_bar` is the ground
  truth outbox recovery compares against.

**Writable / persistent integration state** — the `state` mount:

- `/app/outbox/outbox.db` and its `-wal` / `-shm` sidecars.
- **PREPARED, READY and HELD rows must survive container recreation.** They are
  the record of signals the backend has not yet acknowledged. Losing them drops
  execution-critical CLOSE signals, and a lost CLOSE leaves a real customer
  position open with no stop — the executor places only MARKET orders, so the
  engine's close signal is a position's only exit.

### Warnings

- **Never run an artifact deployment command with `--delete` against
  `signal-engine/state/`.** `rsync --delete`, a wipe-and-recopy, or a
  "clean redeploy" there destroys undelivered signals. Stage artifacts into
  `runtime/` only; the two trees are separate precisely so this cannot happen by
  accident.
- **A `HELD` row blocks the signal lane and requires operator investigation.**
  Held means either a payload divergence on recovery or an orphan whose bar left
  the engine's fetch window. The lane stops on purpose: later signals must not be
  delivered around an undelivered one. The heartbeat reports `status="error"`
  naming the stuck `bar_time`.
- **Do not delete the outbox to "fix" a stuck lane.** That converts a loud,
  recoverable stall into silent data loss. Read the row, resolve the underlying
  cause, then let delivery resume.

## Layout

`find_csv()` globs `BASE_DIR` **itself**, so the four historical CSVs sit at
the top level of `runtime/`, not under `model files/`:

```
runtime/                                       <- BASE_DIR (= ENGINE_BASE_DIR)
  ETHUSDT_15m_BINANCE_20230401_20260401_clean_raw_plus_external.csv
  ETHUSDT_1h_BINANCE_20230401_20260401_clean_raw_plus_external.csv
  ETHUSDT_4h_BINANCE_20230401_20260401_clean_raw_plus_external.csv
  ETHUSDT_1d_BINANCE_20230401_20260401_clean_raw_plus_external.csv
  eth_feature_shortlist_outputs/
    ethusdt_feature_shortlist_best3_global.csv
  model files/
    ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib
    ethusdt_15m_short_expansion_mandatory_ml_config.json
    ethusdt_15m_v22_long_ml_model.joblib
    ethusdt_15m_v22_long_ml_features.json
    ethusdt_15m_short_no_filter_ml_model.joblib
    ethusdt_15m_short_no_filter_ml_features.json
    ethusdt_15m_v22_selected_row.json
    ethusdt_15m_v22_final_export_audit_summary.json
    ethusdt_15m_v22_final_comparison_rows.csv
    ethusdt_15m_v22_final_ml_taken_trades.csv
    v22_live_engine_export/run_20260617_234524_821089_utc/
      v22_live_decision_engine_config.json
      v22_live_long_candidate_engine.json
      v22_live_engine_parity_summary.json
      v22_live_engine_candidate_audit.csv
    runtime_cache/                             <- docker volume
    shadow_live_v22_candidate_match_audit/     <- docker volume
```

Two things here are load-bearing and easy to get wrong:

- The export run directory **must start with `run_`**. `find_latest_v22_engine_export_run()`
  ignores anything else and raises `No V22 live engine export runs found`.
- The audit directory must contain **only** the five allowed audit filenames.
  `initialize_clean_audit_files()` raises on any unexpected file in it.

All 10 model artifacts are hashed at startup. Stage them as a complete set —
mixing in even one file from the previous generation in `worker/runtime` fails
the boot.

## Required environment

See `.env.example`. `ENGINE_SERVICE_TOKEN` must match the frontend's value; it
is a different secret from the executor's `ENGINE_CREDENTIALS_TOKEN`.
`ENGINE_USER_ID` is normally left empty (broadcast to every running client).

## Staging artifacts

`runtime/` is git-ignored and **not** copied into the image, so the build never
depends on it and rotating one artifact never means rebuilding. Stage it on the
host instead:

```bash
sudo mkdir -p /opt/trading-bot/signal-engine/runtime \
              /opt/trading-bot/signal-engine/state
# then upload the artifact tree into runtime/ (see ARTIFACT_UPLOAD_CHECKLIST.md)
```

Leave `state/` empty — the engine creates the outbox on first boot.

All 10 model artifacts are hashed at startup, so stage them as a complete set:
mixing in even one file from the previous generation under `worker/runtime`
fails the boot.

## Running

Start is opt-in:

```bash
cp .env.example .env      # then edit with real values
docker compose --profile engine up -d
```

A bare `docker compose up -d` starts nothing, on purpose: this engine posts to
the production signal API as soon as it completes a live bar.

Before starting it, confirm the previous-generation `v22-engine` container is
stopped. Two engines posting for the same bar is a correctness failure.

## First boot

Expect a slow, network-heavy first start, and watch the `[STARTUP PROGRESS]`
and `[STARTUP VERIFICATION]` lines:

- Open Interest bootstraps ~99 days (Binance Metrics daily files plus a 29-day
  REST tail); RealAgg bootstraps 4 completed days of aggTrades daily zips. The
  `realagg_cache` volume makes this a first-boot cost rather than a
  per-restart one.
- Startup loads the historical panel three times (threshold build, local
  feature audit, training fingerprint) and replays 20 000 bars through the ML
  path for the fingerprint. This is the peak memory moment.
- The first cycle runs in `CATCHUP` mode, which replays the whole fetched
  window. `ingester.py` deliberately does not emit signals for `CATCHUP` bars —
  otherwise the executor would receive a burst of stale entries.
