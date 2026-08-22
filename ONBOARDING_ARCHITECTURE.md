# Dynamic client onboarding

How a client goes from signup to a live Binance order with no operator involved,
and what keeps one client's money separate from another's.

## What was blocking it

The executor took a single `ENGINE_USER_ID` from its environment, so onboarding
meant an operator editing a `.env` and restarting a container. The ML worker had
the same problem: `worker/ingester.py` stamped every signal with one user id, so
a new client received no signals at all. A client could sign up, connect keys,
configure and press Start, and nothing whatsoever would happen.

## Architecture: multi-tenant executor (Option B)

Rejected Option A (one auto-provisioned container per client) because it requires
the public-facing Node app to hold Docker socket access — root-equivalent on the
VPS. Introducing a container-escape-grade privilege to solve a key-isolation
problem is a bad trade. Two further reasons:

- `live_code.py` is one frozen ETHUSDT strategy. Signals are derived from the
  market, not the user, so per-client workers would recompute identical output.
  The fan-out is needed either way.
- The isolation that matters — never sign A's order with B's keys — is
  enforceable *and testable* in-process. Container orchestration mostly isn't.

```
signup ──► trigger creates engine_config + engine_status  (already existed)
  │
  ├─► /app/connect  ──► AES-256-GCM ──► binance_keys
  │
  ├─► /app/engine   ──► execution_mode = LIVE_READ | LIVE_TRADE
  │                     is_running = true
  │
  ▼
GET /api/public/engine/users/active   ◄── executor polls, every cycle
  │  { users: [{user_id}, …] }
  ▼
one UserSession per client ── own credentials ── own Binance client
                           ── own consumer (cursor, sizing, reconciler)
                           ── own risk guard
```

Worker side, in parallel:

```
live_code.py ─► ingester (no user_id) ─► POST /ingest/signal
                                          └─► fan out to every is_running client
```

### One trading cycle, not two

`UserSession.run_cycle()` in `executor/user_session.py` is the *only*
implementation of the trading cycle. Both the single-user path
(`_run_single_user`) and the multi-tenant loop drive it. A safety gate enforced
in one loop and forgotten in the other would be worse than the bug this replaced,
so there is deliberately no second copy.

### Roster grants attention, never capability

Appearing on the roster means only that the executor builds a session and starts
reporting telemetry. Whether that client may place an order is still decided per
cycle, per user, by the executor: the host `.env` ceiling, their `execution_mode`,
`auto_execute_enabled`, `is_running`, `live_order_cap_usd`, `LIVE_TRADING_ACK`,
and whether their keys resolve. Nothing in the roster can raise any of those.

### Two rosters, deliberately

| | Gate | Why |
| --- | --- | --- |
| **Signal subscribers** | `is_running = true` | A `signal_only` client watches the strategy without executing it. Gating on `execution_mode` would blank their dashboard. |
| **Execution roster** | `execution_mode <> 'OFF'` | **Not** gated on `is_running`. A client who presses Stop stays on the roster so their session keeps heartbeating and the Engine page shows `kill_switch_active` rather than a stale heartbeat. |

## Isolation guarantees

Everything that could carry one client's identity, funds or position into
another's decision lives on the `UserSession` instance and nowhere else:
credentials, Binance client, consumer, risk guard, exchange-state flags,
effective mode, failure counter. There is no module-level mutable state, no
shared client, and each session gets its **own** `UserCredentialsClient` bound to
its own user id.

What is shared is read-only: the host's `.env` ceiling. The environment remains
the ceiling for every user; the database can only ever narrow it.

### Failure containment

| Event | Blast radius |
| --- | --- |
| One client's cycle raises | Counted against that client, reported on their telemetry, loop continues |
| Unforeseen exception | Caught by a deliberate catch-all — degrades one client, never the process |
| 10 consecutive failures | That client is **parked**; every other client keeps trading |
| `FatalConfigError` | Parks one client (the single-user executor exits — right for one account, wrong for many) |
| Client's keys missing | That session fails closed with `missing_user_binance_keys`; others unaffected |
| Session cannot be constructed | Logged and skipped; the other sessions are still built |
| Roster fetch fails | **Nothing changes.** Existing sessions keep running — emptying the roster on a network blip would stop trading for everyone at once |
| Client removed from roster | Session dropped, and its Binance client with it |

## Trade history: three distinct layers

Never merged, because collapsing them is how a client comes to believe a paper
result was a real one.

| Layer | Table | Meaning |
| --- | --- | --- |
| Strategy | `user_signals`, `user_trades` | What the model decided |
| Exchange intent | `engine_orders`, status `INTENT_LOGGED` / `DRYRUN` / `SKIPPED` | Recorded, **never sent** |
| Exchange reality | `engine_orders`, status `SENT` / `FILLED` / `FAILED` | Actually reached Binance |

The History page renders the strategy layer and a separate **Binance orders**
table with plain-language outcomes ("Not sent — blocked", "Filled on Binance",
"Rejected by Binance") plus counts of sent vs. filled.

## Idempotency

The worker retries on timeout. A broadcast retry would otherwise deliver a second
copy of the same bar to every client, which the executor would read as a second
signal and could act on. The fan-out excludes users who already hold a row for
that `bar_time`, so redelivery is a no-op.

A `UNIQUE (user_id, bar_time)` index would make this a database guarantee and is
recommended as a follow-up — it is deliberately not in the migration because
existing production rows may violate it. The check and the statement are in
`supabase/migrations/20260822130000_multi_tenant_execution.sql`.

## Scaling limits

- Roster capped at `MAX_ROSTER_USERS = 500`; truncation is reported in the
  endpoint response, never silently applied.
- Clients are processed sequentially. A slow client costs the others latency but
  never correctness. At roughly 3 signed Binance calls per client per cycle,
  a 60-second cycle is comfortable into the low hundreds of clients. Beyond that,
  shard by running several executors with disjoint roster slices — the sharding
  key would go on the roster endpoint, not in the loop.
- Binance rate limits are per API key, so one client's limit does not consume
  another's.

## Tests

| File | Covers |
| --- | --- |
| `executor/test_multi_tenant.py` | Onboarding with no env change; mid-run add/remove; **each session signs only with its own keys**; per-user failure/parking containment; per-user kill switch; roster parsing and outage handling |
| `src/lib/engine-roster.test.ts` | Eligibility (too-narrow and too-wide directions); dedupe; cap; roster endpoint leaks no key material or metadata; fan-out targets subscribers not the execution roster; idempotency |
| `executor/test_user_credentials.py` | Per-user credential resolution, fail-closed, no key material in telemetry or logs |
| `src/lib/binance-credentials.test.ts` | One decrypting route; token separation; no secrets in public endpoints |

No test touches a network, and none can create an order.
