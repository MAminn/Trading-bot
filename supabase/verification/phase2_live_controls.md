# Phase 2 verification — live-control read/write path

Phase 2 adds the app layer over the Phase 1 columns: types, validated server
functions, and the Configure/Engine UI. **No executor file was touched and no
order behaviour changed.**

Three layers enforce the same four invariants. They are listed here in order of
authority, because only the last one is load-bearing:

| Layer | Where | Purpose | Authority |
|---|---|---|---|
| UI | `LiveExecutionSection` in `app.configure.tsx` | disables Save, shows the reason | none — convenience |
| Server | `ConfigPatch` + handler in `engine.functions.ts` | clear error messages | none — bypassable only by bypassing the app |
| Database | CHECK constraints + column privileges | the real gate | **authoritative** |

All three call the same `validateLiveState()` in `src/lib/live-controls.ts`, so
they cannot drift apart. The database rules are duplicated in SQL by necessity
and are verified by `phase1_live_controls.sql`.

## Automated — `node --test src/lib/live-controls.test.ts`

28 tests, no test-framework dependency (Node's built-in runner + native TS).
Covers scope items 8.1–8.4:

- **invalid cap rejected** — above 500, negative, NaN, Infinity
- **LIVE_TRADE cap < 25 rejected** — including the default cap of 0, so live
  trading can never be armed by flipping one field
- **live full_capital without consent rejected** — and accepted with it
- **demo + live rejected** — for `LIVE_TRADE` *and* `LIVE_READ`
- plus: unknown modes fail closed and stop further rules rather than being
  treated as OFF; partial patches never report an unevaluable rule as
  satisfied; merged-state patches (demo-only, sizing-only) are still caught.

## Manual — item 8.5: `mode: auto` writes through the server function

Requires a running app and a logged-in session. `mode` is one of the four
columns Phase 1 revoked, so this specifically exercises the service-role path.

1. Configure → Live execution → toggle **Auto-execute** on → **Save live settings**.
2. Expect the success toast, and the "Saved:" line to read `… · auto on`.
3. Confirm in the database that the generated column followed:

   ```sql
   SELECT mode, auto_execute_enabled, execution_mode, live_order_cap_usd
   FROM public.engine_config WHERE user_id = '<your-uuid>';
   -- expect: mode='auto', auto_execute_enabled=true
   ```

4. Toggle it back off and re-save; `auto_execute_enabled` must return to false.

Pass criteria: the write succeeds (proving the service-role path works) and
`auto_execute_enabled` always equals `mode = 'auto'` (proving it is generated,
never independently set).

## Manual — item 8.6: a direct user write is still blocked

The point of the server function is that it is the *only* way in. Confirm the
database still refuses the browser's own credentials — run this in the browser
console while logged in:

```js
const { error } = await window.supabase
  .from("engine_config")
  .update({ execution_mode: "LIVE_TRADE", live_order_cap_usd: 500 })
  .eq("user_id", (await window.supabase.auth.getUser()).data.user.id);
console.log(error);   // expect a 42501 / "permission denied for column" error
```

If `window.supabase` is not exposed, use any REST client with the session's
access token against `PATCH /rest/v1/engine_config?user_id=eq.<uuid>`.

This is already proven non-interactively by sections **D** and **E** of
`supabase/verification/phase1_live_controls.sql`, which assert both the
privilege catalog state and a real refused `UPDATE` executed as the
`authenticated` role. Re-running that script is the faster check.

## Regression checks

- **Start/Stop** still works — `setEngineRunning` writes `is_running` with the
  user's JWT and was not modified; `is_running` remains in the Phase 1 column
  grant.
- **Configure → Save** (sizing) still works — a sizing-only patch touches
  `sizing_mode`, so it now takes the merged-state validation branch, but is
  still written with the user's JWT under RLS. The only new failure mode is a
  clear error when the resulting state would be illegal (e.g. switching to
  full_capital while the row requests LIVE_TRADE without consent).
- **Demo toggle** on the Engine page now performs one extra read before the
  write, so demo + live is refused with a readable message instead of a raw
  constraint violation.
