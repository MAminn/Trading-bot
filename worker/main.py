"""
ETHUSDT V22 Engine — Worker entrypoint.

This module imports the frozen `engine/live_code.py` (your full V22_LONG +
SHORT_NO_FILTER + Mandatory ML engine). live_code is the *real* engine,
unchanged except that BASE_DIR is now read from the `ENGINE_BASE_DIR` env var.

Before delegating to live_code.main(), we monkey-patch
`live_code.append_csv_row` so that EVERY new signal / trade / position row
is also POSTed to the app ingest endpoints. This makes the
dashboard reflect the exact decisions of the frozen engine, in real time.

Required environment:
  ENGINE_BASE_DIR   = /app/runtime           (set by Dockerfile)
  APP_API_BASE      = https://YOUR_DOMAIN_OR_SERVER_IP
  ENGINE_SERVICE_TOKEN = <bearer token, same value as the frontend secret>
  ENGINE_USER_ID    = <Supabase auth user UUID — which account owns the signals>
"""
from __future__ import annotations

import logging
import os
import sys

# numpy must be imported first — the calibrator classes below depend on it.
import numpy as np


# =============================================================================
# PICKLE COMPATIBILITY
# -----------------------------------------------------------------------------
# The model bundle was pickled with these classes under `__main__` during
# training. Because this file (main.py) is `__main__` at runtime, defining them
# here — BEFORE importing engine.live_code — lets joblib's unpickler resolve
# `__main__.IdentityCalibrator` / `__main__.BetaCalibratorWrapper`. The
# module-level `joblib.load` inside engine/live_code.py runs at import time, so
# these definitions MUST come first. Keep them identical to the copies in
# engine/live_code.py (lines 60–90).
# =============================================================================
class IdentityCalibrator:
    def __init__(self, estimator):
        self.estimator = estimator

    def predict_proba(self, X):
        p = np.clip(self.estimator.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        return np.column_stack([1.0 - p, p])


class BetaCalibratorWrapper:
    def __init__(self, estimator):
        self.estimator = estimator
        self.model = None

    @staticmethod
    def _beta_features(p):
        p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
        return np.column_stack([np.log(p), np.log(1.0 - p)])

    def fit(self, X, y):
        from sklearn.linear_model import LogisticRegression
        p = np.clip(self.estimator.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        z = self._beta_features(p)
        self.model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
        self.model.fit(z, y)
        return self

    def predict_proba(self, X):
        p = np.clip(self.estimator.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        z = self._beta_features(p)
        pp = np.clip(self.model.predict_proba(z)[:, 1], 1e-6, 1 - 1e-6)
        return np.column_stack([1.0 - pp, pp])


# Import the frozen engine AFTER the calibrator classes exist in __main__, so
# the module-level joblib.load inside engine/live_code.py can unpickle cleanly.
import importlib
sys.path.insert(0, os.path.dirname(__file__))
live_code = importlib.import_module("engine.live_code")

from ingester import attach_ingester

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

if __name__ == "__main__":
    attach_ingester(live_code)
    log.info("[boot] ENGINE_BASE_DIR=%s APP_API_BASE=%s",
             os.environ.get("ENGINE_BASE_DIR"),
             os.environ.get("APP_API_BASE") or os.environ.get("LOVABLE_API_BASE"))
    # Historical CSVs serialize cvd cumsum features at ~2^-16 granularity; observed
    # max drift 1.53e-05 on multi-million-magnitude values (relative ~1e-11).
    # Keep AUDIT_STRICT=True; widen absolute tolerance just past the quantum.
    live_code.AUDIT_TOL = 5e-5
    live_code.main()
