"""ETHUSDT V22 candidate-match signal engine — worker entrypoint.

This module imports the model owner's delivered engine (`engine/live_code.py`)
and bridges it to the existing production signal API. The engine itself is the
strategy source of truth and is not modified beyond the two infrastructure
adaptations documented in `engine/__init__.py`.

Architecture (unchanged by this worker):

    signal/model engine  ->  website/backend signal API  ->  separate
    multi-tenant executor  ->  customer Binance USD-M Futures accounts

This process is the first box only. It holds no customer Binance credentials,
sizes no positions, sets no allocation or leverage, and places no orders. The
executor remains solely responsible for all of that.

Required environment:
  ENGINE_BASE_DIR      = /app/runtime            (set by the Dockerfile)
  APP_API_BASE         = https://YOUR_DOMAIN_OR_SERVER_IP
  ENGINE_SERVICE_TOKEN = <bearer token, same value as the frontend secret>
  ENGINE_USER_ID       = <optional; empty = broadcast to every running client>
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
# training — the bundle's own bytes reference `__main__.IdentityCalibrator`.
# Because this file (main.py) is `__main__` at runtime, defining them here,
# BEFORE importing engine.live_code, lets joblib's unpickler resolve them. The
# module-level `joblib.load` inside engine/live_code.py runs at import time, so
# these definitions MUST come first.
#
# Keep them byte-identical to the copies in engine/live_code.py (lines 80-111).
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


# Import the engine AFTER the calibrator classes exist in __main__, so the
# module-level joblib.load inside engine/live_code.py can unpickle cleanly.
import importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
live_code = importlib.import_module("engine.live_code")

from ingester import attach_ingester

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("signal-engine")

if __name__ == "__main__":
    # NOTE: unlike the previous-generation worker, this entrypoint deliberately
    # does NOT override live_code.AUDIT_TOL. The delivered engine sets
    # AUDIT_TOL = 1e-6 and carries its own STARTUP_FEATURE_AUDIT_SKIP_COLUMNS
    # exemption list for the known historical CVD/flow rebase. Loosening that
    # tolerance would weaken the model owner's startup feature audit, which is
    # a strategy-validation decision and not ours to make silently.

    attach_ingester(live_code)
    log.info(
        "[boot] ENGINE_BASE_DIR=%s APP_API_BASE=%s",
        os.environ.get("ENGINE_BASE_DIR"),
        os.environ.get("APP_API_BASE") or os.environ.get("LOVABLE_API_BASE"),
    )
    live_code.main()
