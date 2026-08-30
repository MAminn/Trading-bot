"""Package marker for the canonical signal/model engine.

`live_code.py` beside this file is the model owner's delivered final engine,
copied byte-for-byte. Its trading and model logic is frozen: model features,
thresholds, candidate logic, RealAgg logic, OI logic, HTF logic, ML inference,
entry/exit logic, the global no-overlap policy, LONG_FIRST and the no-flip
policy are not modified here.

Two infrastructure-only adaptations are applied to `live_code.py` for
production, and nothing else:

  1. BASE_DIR is read from the ENGINE_BASE_DIR environment variable instead of
     the model owner's local macOS path.
  2. The hard-coded email credential defaults are emptied, so alert
     credentials come from the environment or the alerts stay off.

Both sit outside every function fingerprinted by the engine's own
EXPECTED_CRITICAL_LOGIC_SHA256 startup guard, which hashes the source of the 13
critical trading/model functions and refuses to start if any has changed.

The engine emits no HTTP itself and places no orders. Its own
`_startup_no_order_scan` fails startup if `requests.post(`, `requests.put(`,
`requests.delete(`, `requests.patch(`, `/fapi/v1/order` or `/api/v3/order`
appears in the source of any function in this module's globals. Signal
emission therefore lives in `../ingester.py`, outside this package.
"""
