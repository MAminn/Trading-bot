"""
ETHUSDT V22 Engine — Worker entrypoint.

This module imports the frozen `engine/live_code.py` (your full V22_LONG +
SHORT_NO_FILTER + Mandatory ML engine). live_code is the *real* engine,
unchanged except that BASE_DIR is now read from the `ENGINE_BASE_DIR` env var.

Before delegating to live_code.main(), we monkey-patch
`live_code.append_csv_row` so that EVERY new signal / trade / position row
is also POSTed to the Lovable Cloud ingest endpoints. This makes the
dashboard reflect the exact decisions of the frozen engine, in real time.

Required environment:
  ENGINE_BASE_DIR   = /app/runtime           (set by Dockerfile)
  LOVABLE_API_BASE  = https://project--<id>.lovable.app
  ENGINE_SERVICE_TOKEN = <bearer token, same value as the Lovable secret>
  ENGINE_USER_ID    = <Supabase auth user UUID — which account owns the signals>
"""
from __future__ import annotations

import logging
import os
import sys

# Make IdentityCalibrator / BetaCalibratorWrapper resolvable for joblib unpickling.
# The training script defined them in __main__, so we register them there before load.
import importlib
sys.path.insert(0, os.path.dirname(__file__))

# live_code unpickles classes that were created in __main__ at training time.
# Importing it as a regular module is fine because both classes are defined
# inside live_code itself — joblib looks them up via the bundle's `__module__`
# attribute. If the training save recorded "__main__", we map __main__ to
# live_code so unpickling resolves correctly.
live_code = importlib.import_module("engine.live_code")
import __main__ as _main_mod
for _name in ("IdentityCalibrator", "BetaCalibratorWrapper"):
    if hasattr(live_code, _name) and not hasattr(_main_mod, _name):
        setattr(_main_mod, _name, getattr(live_code, _name))

from ingester import attach_ingester

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

if __name__ == "__main__":
    attach_ingester(live_code)
    log.info("[boot] ENGINE_BASE_DIR=%s LOVABLE_API_BASE=%s",
             os.environ.get("ENGINE_BASE_DIR"), os.environ.get("LOVABLE_API_BASE"))
    live_code.main()
