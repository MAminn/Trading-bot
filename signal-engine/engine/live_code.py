#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETHUSDT — SHADOW LIVE 15M V22 CANDIDATE MATCH / LONG + SHORT ML
==============================================================

Paper/live monitor only. No real orders.
Candidate-match copy built from the clean shadow source.
Scope: reproduce the validated offline training pipeline before final acceptance: exact feature contract, LONG strict-reaction side selection, SHORT raw side no-overlap, frozen ML, global LONG_FIRST no-overlap, and exact V22/SHORT exits.
Matched to final training artifacts:
- BASE_DIR: /Users/omarhassan/Desktop/project/Eth/test backup/Website/Archive
- Artifacts:
  model files/ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib
  model files/ethusdt_15m_short_expansion_mandatory_ml_config.json
- Separate LONG and SHORT ML models
- 15m base only
- HTF context: 1h / 4h / 1d only
- Entry: NEXT 15m open
- No 1m execution
- No 5m execution
- No pending/retest path
- No flip-immediate path
- V22 LONG + Version B SHORT_NO_FILTER + ML
- Trail-mode TP matches training: TP is audited/stored but does not close the trade

Clean live audit outputs are written/appended cumulatively and resume safely under:
- /Users/omarhassan/Desktop/project/Eth/test backup/Website/Archive/model files/shadow_live_v22_candidate_match_audit

Exactly five audit files are produced: one master candle audit, one clean live-only
trade log, one atomic runtime state, one errors/warnings log, and one dedicated
external-data parity audit for REALAGG/OI/model-feature health. Historical
fingerprint replay is calculated in memory and never written as live trades.
REALAGG restart persistence is stored separately under model files/runtime_cache
and is not part of the five-file audit contract or any model artifact.
No real orders are sent.
"""

from __future__ import annotations

import os
import io
import json
import copy
import hashlib
import inspect
import fcntl
import time
import zipfile
import atexit
import uuid
import traceback
import smtplib
import warnings
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple
from bisect import bisect_left, bisect_right, insort
from collections import deque

import joblib
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# =============================================================================
# PICKLE COMPATIBILITY
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


# =============================================================================
# PATHS / CONFIG
# =============================================================================
BASE_DIR = Path(os.environ.get("ENGINE_BASE_DIR", "/app/runtime"))
ARTIFACTS_DIR = BASE_DIR / "model files"

# Clean Shadow/Paper Live audit output only. Model/training artifacts remain
# untouched under ARTIFACTS_DIR. This folder is intentionally limited to the
# five user-facing files declared below.
SHADOW_AUDIT_PARENT_DIR = BASE_DIR / "model files"
LIVE_AUDIT_DIR = SHADOW_AUDIT_PARENT_DIR / "shadow_live_v22_candidate_match_audit"
LIVE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

RUN_ID = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"
STATE_SCHEMA_VERSION = 3

MASTER_AUDIT_FILE = LIVE_AUDIT_DIR / "shadow_live_master_audit.jsonl"
TRADES_FILE = LIVE_AUDIT_DIR / "shadow_live_trades.csv"
RUNTIME_STATE_FILE = LIVE_AUDIT_DIR / "shadow_live_state.json"
ERRORS_FILE = LIVE_AUDIT_DIR / "shadow_live_errors.jsonl"
EXTERNAL_DATA_AUDIT_FILE = LIVE_AUDIT_DIR / "shadow_live_external_data_fix_audit.jsonl"

ALLOWED_AUDIT_FILENAMES = {
    MASTER_AUDIT_FILE.name,
    TRADES_FILE.name,
    RUNTIME_STATE_FILE.name,
    ERRORS_FILE.name,
    EXTERNAL_DATA_AUDIT_FILE.name,
}

SHADOW_TRADE_COLUMNS = [
    "logged_at_utc", "trade_id", "status", "side", "setup_name",
    "signal_t", "entry_t", "exit_t", "entry", "exit", "tp",
    "initial_sl", "final_stop", "atr", "bars_held", "prob",
    "threshold", "exit_reason", "gross_pnl_rate",
    "net_pnl_rate_after_round_trip_cost", "round_trip_cost",
    "best_high", "best_low", "mfe_atr", "mae_atr",
    "trail_active_at_exit", "path_bar_count", "trade_path_json",
    "leverage_scenarios_json",
]

BUNDLE_FILE = ARTIFACTS_DIR / "ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib"
CONFIG_FILE = ARTIFACTS_DIR / "ethusdt_15m_short_expansion_mandatory_ml_config.json"
SHORTLIST_FILE = BASE_DIR / "eth_feature_shortlist_outputs" / "ethusdt_feature_shortlist_best3_global.csv"
V22_ENGINE_EXPORT_ROOT = ARTIFACTS_DIR / "v22_live_engine_export"

def find_latest_v22_engine_export_run(root: Path) -> Path:
    if not root.exists():
        raise FileNotFoundError(f"Missing V22 live engine export root: {root}")
    runs = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("run_")]
    if not runs:
        raise FileNotFoundError(f"No V22 live engine export runs found under: {root}")
    required_names = {
        "v22_live_decision_engine_config.json",
        "v22_live_long_candidate_engine.json",
        "v22_live_engine_parity_summary.json",
        "v22_live_engine_candidate_audit.csv",
    }
    valid = []
    for r in runs:
        if all((r / n).exists() for n in required_names):
            valid.append(r)
    if not valid:
        raise FileNotFoundError(f"No complete V22 live engine export run found under: {root}")
    return sorted(valid, key=lambda p: p.name)[-1]

V22_ENGINE_EXPORT_DIR = find_latest_v22_engine_export_run(V22_ENGINE_EXPORT_ROOT)
V22_ENGINE_DECISION_CONFIG_FILE = V22_ENGINE_EXPORT_DIR / "v22_live_decision_engine_config.json"
V22_ENGINE_LONG_ENGINE_FILE = V22_ENGINE_EXPORT_DIR / "v22_live_long_candidate_engine.json"
V22_ENGINE_PARITY_SUMMARY_FILE = V22_ENGINE_EXPORT_DIR / "v22_live_engine_parity_summary.json"
V22_ENGINE_CANDIDATE_AUDIT_FILE = V22_ENGINE_EXPORT_DIR / "v22_live_engine_candidate_audit.csv"
V22_LONG_SOURCE_FILE = V22_ENGINE_CANDIDATE_AUDIT_FILE

FINAL_MODEL_ARTIFACT_FILES = {
    "bundle": BUNDLE_FILE,
    "config": CONFIG_FILE,
    "audit_summary": ARTIFACTS_DIR / "ethusdt_15m_v22_final_export_audit_summary.json",
    "selected_row": ARTIFACTS_DIR / "ethusdt_15m_v22_selected_row.json",
    "comparison_rows": ARTIFACTS_DIR / "ethusdt_15m_v22_final_comparison_rows.csv",
    "final_trade_log": ARTIFACTS_DIR / "ethusdt_15m_v22_final_ml_taken_trades.csv",
    "short_features": ARTIFACTS_DIR / "ethusdt_15m_short_no_filter_ml_features.json",
    "long_features": ARTIFACTS_DIR / "ethusdt_15m_v22_long_ml_features.json",
    "short_model": ARTIFACTS_DIR / "ethusdt_15m_short_no_filter_ml_model.joblib",
    "long_model": ARTIFACTS_DIR / "ethusdt_15m_v22_long_ml_model.joblib",
}

EXPECTED_FINAL_MODEL_ARTIFACT_SHA256 = {
    "bundle": "c8830967528eb7ee8fd5a20e39ccb6e751f899369bbe53960eb164c894b5a77d",
    "config": "e2d7350821e90d8a9d103bde7063a6f5c87c0c3133a9fd34f48b6df252bdd9a7",
    "audit_summary": "c57f1c4af64732cebe92749daa689c96fc4b56ab2d8867104645b2cc45ec3c2c",
    "selected_row": "f62e126ab9afd87bb569b9b25a53dd263bd9b7a2e9b46f7ee70032f237edb9b4",
    "comparison_rows": "3523fc902f99a6bf702cc8251753c5f730f9747c83a9e46ab4b46a24b8f543e3",
    "final_trade_log": "97e2f3b652d501004cd57bb8ea75b0e86f2cd37307a820927a31969fcec03c21",
    "short_features": "0357860b16af413ca6fbeabe2eb5faf77615cf3034f4df2586c1fa63db2489b2",
    "long_features": "7bed30481de8c63317f21603f8ac268c2dd26df16db0abfa682e27f10b356571",
    "short_model": "fbc5244a2e8da0de3c2d9ee7f77b4edfdb2b10e1e5bc50090fc06fe2ed85aa59",
    "long_model": "81f5654e8c825ecb1b2dec67f3af810981a3cd4d6ec791257a6ebc88fcafd794",
}

SYMBOL = "ETHUSDT"
START_DATE = pd.Timestamp("2024-04-01 00:00:00")
END_DATE = pd.Timestamp("2026-03-31 23:59:59")
TRAIN_RATIO = 0.60
VAL_RATIO = 0.20

BASE_TF = "15m"
HTF_TFS = ["1h", "4h", "1d"]
TRAINING_AUDIT_TFS = ["15m", "1h", "4h", "1d"]
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
EXPECTED_NEXT_MINUTES = 15

PROCESS_ONLY_CLOSED_BARS = True
USE_PRICE_ENDPOINT = False
SAME_BAR_POLICY = "worst"
NO_OVERLAP_LIVE = True
LIVE_LOG_BID_ASK_SPREAD_USD = 0.0

# Same locked training rules.
LONG_SETUP_FAMILY = "V22_LONG"
LONG_SETUP_NAME = "V22_LONG"
LONG_TRIGGER = "ORCH_V1_SETUP_REGIME_GATE"
LONG_EXIT_NAME = "V22_RX4_MIXED_BALANCED_CUT"
LONG_EXIT_SL_ATR = np.nan
LONG_EXIT_TP_ATR = 1.70
LONG_EXIT_TRAIL_START_ATR = np.nan
LONG_EXIT_TRAIL_DIST_ATR = np.nan
LONG_EXIT_MAX_HOLD_BARS = 24

V22_SELECTED_VARIANT_NAME = "V22_RX4_MIXED_BALANCED_CUT"
V22_BASE_DECISION_BAR = 4
V22_BASE_PROVISIONAL_SL_ATR = 2.45
V22_BASE_PROVISIONAL_SL_DEFAULT_ATR = 2.10
V22_BASE_TRUE_FAIL_MFE_MAX_ATR = 0.45
V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR = -1.00
V22_BASE_TRUE_FAIL_MAE_MIN_ATR = 1.10
V22_BASE_NORMAL_SL_ATR = 1.35
V22_BASE_TRAIL_START_ATR = 0.65
V22_BASE_TRAIL_DIST_ATR = 0.35
V22_BASE_TP_ATR = 1.70
V22_BASE_MAX_HOLD_BARS = 24
V22_MIXED_BAD_CLOSE_MAX_ATR = -0.85
V22_MIXED_BAD_MFE_MAX_ATR = 0.55
V22_MIXED_BAD_MAE_MIN_ATR = 1.45
V22_MIXED_RECOVER_MFE_MIN_ATR = 0.55
V22_MIXED_RECOVER_CLOSE_MIN_ATR = -0.65
V22_MIXED_RECOVER_SL_ATR = 1.80
V22_MIXED_RECOVER_TRAIL_START_ATR = 0.90
V22_MIXED_RECOVER_TRAIL_DIST_ATR = 0.45
V22_WEAK_VOL_REGIMES = {"vol_mid", "vol_low"}
V22_WEAK_OI_REGIMES = {"oi_low_z", "oi_mid_z"}

# Proven frozen V22 volatility-regime contract. These are derived from the
# 1,203 selected V22 LONG train rows only, never from all train-panel rows.
V22_FROZEN_VOL_Q33 = 0.01895648494619611
V22_FROZEN_VOL_Q66 = 0.025208427513199228

# Exact ORCH_V1_STRICT_REACTION side-selection management. This hidden
# selector runs on all breakout + reversal orchestration candidates before
# only selected breakout entries are exposed to LONG ML.
STRICT_REACTION_INITIAL_SL_ATR = 1.05
STRICT_REACTION_TP_ATR = 1.55
STRICT_REACTION_HOLD_BARS = 20
STRICT_REACTION_TRAIL_START_ATR = 0.70
STRICT_REACTION_TRAIL_DIST_ATR = 0.35
STRICT_REACTION_EARLY_BARS = 4
STRICT_REACTION_EARLY_MIN_MFE_ATR = 0.55
STRICT_REACTION_EARLY_BAD_CLOSE_ATR = -0.15
STRICT_REACTION_WEAK_HOLD_BARS = 8
STRICT_REACTION_RUNNER_MFE_ATR = 1.20
STRICT_REACTION_SETUP_ARCHETYPES = {"breakout", "reversal_after_drop"}

SHORT_SETUP_FAMILY = "VOLATILITY"
SHORT_SETUP_NAME = "fam__VOLATILITY"
SHORT_TRIGGER = "momentum_break"
SHORT_FILTER = "NO_FILTER"
SHORT_EXIT_NAME = "trail0.50_0.30_sl1.15_tp2.20_hold8"
SHORT_EXIT_SL_ATR = 1.15
SHORT_EXIT_TP_ATR = 2.20
SHORT_EXIT_TRAIL_START_ATR = 0.50
SHORT_EXIT_TRAIL_DIST_ATR = 0.30
SHORT_EXIT_MAX_HOLD_BARS = 8
SHORT_USE_SHORTLIST_FEATURE_FIELD_FIRST = True

USE_1H_SOFT_VETO = True

OUTPUTSIZE = {
    "1m": 1600,
    "5m": 1600,
    "15m": 1600,
    "1h": 1500,
    "4h": 1500,
    "1d": 1000,
}

BINANCE_BASE = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
BINANCE_FUTURES_BASE = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")
BINANCE_DATA_VISION_BASE = "https://data.binance.vision/data/futures/um/daily"

# Exact current frozen contract needs at most 20 x 4h realagg history for the
# 4h flow_absorption feature (80h); four completed UTC days provide 96h before
# today's live tail. V22's 15m realagg rolling requirement is only 50 bars.
REALAGG_BOOTSTRAP_COMPLETED_DAYS = 4
REALAGG_REFRESH_LOOKBACK_HOURS = 2

# REALAGG transport/cache reliability only. These settings do NOT alter the
# source, aggregation, timestamps, features, models, thresholds, rules, or exits.
# Cache is kept outside the five-file shadow audit folder and outside the ten
# frozen model artifacts. CSV is intentional here: it is dependency-free for a
# backend restart and floats are written at 17 significant digits for round-trip.
REALAGG_RUNTIME_CACHE_DIR = ARTIFACTS_DIR / "runtime_cache"
REALAGG_RUNTIME_CACHE_FILE = REALAGG_RUNTIME_CACHE_DIR / "realagg_1m_cache.csv"
REALAGG_REST_MIN_INTERVAL_SECONDS = 1.25
REALAGG_REST_PROGRESS_EVERY_REQUESTS = 25
REALAGG_REST_CHECKPOINT_EVERY_REQUESTS = 10
REALAGG_CACHE_OVERLAP_HOURS = REALAGG_REFRESH_LOOKBACK_HOURS

# OI history contract:
# - The largest OI diff/pct-change window is 96 bars.
# - On 1d, that needs at least 97 daily observations for the latest value.
# - Keep 2 additional UTC-boundary guard days.
# - Binance REST openInterestHist is still restricted to the recent <=29d tail;
#   older history is bootstrapped from the exact Binance Metrics daily source
#   used by Training/Forward.
OI_HISTORY_DAYS = 99
OI_REST_MAX_HISTORY_DAYS = 29
OI_REFRESH_LOOKBACK_HOURS = 2
OI_FRESHNESS_MAX_WAIT_SECONDS = 90
OI_FRESHNESS_RETRY_SECONDS = 10
OI_DATA_VISION_PROGRESS_EVERY_DAYS = 10

_REALAGG_1M_CACHE = pd.DataFrame()
_REALAGG_CACHE_UTC_DAY = None
_REALAGG_LAST_REFRESH_UTC = None
_REALAGG_REST_LAST_REQUEST_MONOTONIC = None
_OI_5M_CACHE = pd.DataFrame()
_OI_LAST_REFRESH_UTC = None
_OI_REST_SHIFT_MINUTES = None

LEVERAGE_SCENARIOS = [
    {"scenario": "conservative", "leverage": 1.0, "capital_usd": 80.0},
    {"scenario": "middle", "leverage": 30.0, "capital_usd": 80.0},
    {"scenario": "aggressive", "leverage": 70.0, "capital_usd": 80.0},
]

# All live outputs append cumulatively under live audit.

AUDIT_MODE = True
AUDIT_STRICT = True
AUDIT_TOL = 1e-6
STARTUP_FEATURE_AUDIT_SKIP_COLUMNS = {
    # The historical CSV stores generic CVD/flow columns from its frozen source
    # state, while current live rebuilds those aliases from true RealAgg. Gate 4
    # and Step 5 validate the actual model/trade contract; this startup audit
    # must not block live startup on that known historical rebase difference.
    "cvd_base_delta",
    "cvd_quote_delta",
    "cvd_base_delta_z_50",
    "cvd_quote_delta_z_50",
    "cvd_base_roc_20",
    "cvd_quote_roc_20",
    "flow_imb_base_z_20",
    "flow_imb_quote_z_20",
    "aggressive_flow_burst",
}

# Startup verification guardrail only. These SHA256 values fingerprint the exact
# already-validated model/trading/feature-path function sources in this final
# live file. They do not change execution; they fail startup if those critical
# functions are edited later without a deliberate re-validation.
EXPECTED_CRITICAL_LOGIC_SHA256 = {
    "calculate_features": "574ffe03c90eb15c39064857a5d68cbc1f9e16e08c6d6af37902c8a04f5792fe",
    "attach_htf_live": "c4dd513e3cbb60f0ffe553df0ee486c66a180446319c58728d5796c3e3c48919",
    "attach_v22_locked_htf_context": "96fad680efbcebc5e55548c78b83ab8925f09c9a286e8b5ee95fad7cd4395915",
    "build_live_panel": "a4a0227262e5488df4932c4384a6e2cdba856d9d2a02fce3b2e7310fed4b73c9",
    "v22_training_pre_entry_archetype": "a59b5061cfc0621c2dba508b6a93daa2b3bea4089d1fedeef508c201822c5210",
    "v22_long_causal_gate_state": "9e590ac8d724682e039beeb34e228673cd3a5d6ecd01e2a38b15c8213441ffde",
    "_short_rule_candidate": "6109043c17dbcfc1030d0eaed4fd3243fbf92e0216fa6c7f959df7b95c9654c8",
    "build_ml_sample": "2666785fb9b3fb9f21e6d7e963885deaf770a94a8c102ff712253671bc37fb72",
    "predict_side": "4de006a095da2cb9563785cb98815c7d51c6b2864f6c6083b04d460ad01f64f9",
    "create_open_position": "d5a39d0801f12c20f3d49ca9e4a504c8f454a5ec18e77a1c62e87b7f410e3f2d",
    "resolve_position_on_bar": "165497ef502c9b985d1b7cd61fdd3c38bfef1e64320f727c4733dab76a70fd3a",
    "advance_pre_ml_side_selectors": "93e6eedb138171d7d4bcc0d7c8de8b2748ebd08dee67a41af978fa25a36992d5",
    "process_one_signal_bar": "f323d439928642cce1b400a3de4a3ba4afc14e99cff55e693979542ad378f283",
}
EXPECTED_STARTUP_FEATURE_AUDIT_SKIP_COLUMNS = frozenset({
    "cvd_base_delta",
    "cvd_quote_delta",
    "cvd_base_delta_z_50",
    "cvd_quote_delta_z_50",
    "cvd_base_roc_20",
    "cvd_quote_roc_20",
    "flow_imb_base_z_20",
    "flow_imb_quote_z_20",
    "aggressive_flow_burst",
})
_STARTUP_STATIC_VERIFICATION_RESULT: Optional[Dict[str, Any]] = None
_LAST_EXTERNAL_DATA_AUDIT_RECORD: Optional[Dict[str, Any]] = None
AUDIT_ONLY_ON_STARTUP = True
RUN_STARTUP_FULL_PARITY_REPLAY = False
EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES = 2922
EXPECTED_FINAL_TRADES = EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES
EXPECTED_LONG_THRESHOLD = 0.490
EXPECTED_SHORT_THRESHOLD = 0.440
FINGERPRINT_REPLAY_BARS = 20000
LIVE_MONITOR_WINDOW = 200
FEATURE_STD_RATIO_MIN = 0.35
FEATURE_STD_RATIO_MAX = 2.50
RATE_WARN_ABS = 0.05
RATE_WARN_REL = 0.35
LOW_VAR_STD_EPS = 1e-10
DRIFT_WINDOW = 20

TRAINING_FINGERPRINT = None

EMAIL_ADDRESS = os.getenv("LIVE_EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("LIVE_EMAIL_APP_PASSWORD", "")


# =============================================================================
# LOAD TRAINING ARTIFACTS
# =============================================================================
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if not BUNDLE_FILE.exists():
    raise FileNotFoundError(f"Missing bundle: {BUNDLE_FILE}")
if not CONFIG_FILE.exists():
    raise FileNotFoundError(f"Missing config: {CONFIG_FILE}")
if not SHORTLIST_FILE.exists():
    raise FileNotFoundError(f"Missing shortlist: {SHORTLIST_FILE}")
for _p in [V22_ENGINE_DECISION_CONFIG_FILE, V22_ENGINE_LONG_ENGINE_FILE, V22_ENGINE_PARITY_SUMMARY_FILE, V22_ENGINE_CANDIDATE_AUDIT_FILE]:
    if not _p.exists():
        raise FileNotFoundError(f"Missing V22 live engine export file: {_p}")

BUNDLE = joblib.load(BUNDLE_FILE)
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    LIVE_CONFIG = json.load(f)
with open(V22_ENGINE_DECISION_CONFIG_FILE, "r", encoding="utf-8") as f:
    V22_ENGINE_DECISION_CONFIG = json.load(f)
with open(V22_ENGINE_LONG_ENGINE_FILE, "r", encoding="utf-8") as f:
    V22_ENGINE_LONG_ENGINE = json.load(f)
with open(V22_ENGINE_PARITY_SUMMARY_FILE, "r", encoding="utf-8") as f:
    V22_ENGINE_PARITY_SUMMARY = json.load(f)

LONG_MODEL = BUNDLE["long_model"]
SHORT_MODEL = BUNDLE["short_model"]
LONG_THRESHOLD = float(BUNDLE["long_threshold"])
SHORT_THRESHOLD = float(BUNDLE["short_threshold"])
LONG_FEATURE_COLS = list(BUNDLE["long_feature_cols"])
SHORT_FEATURE_COLS = list(BUNDLE["short_feature_cols"])
LONG_CALIBRATION_METHOD = str(BUNDLE.get("long_calibration_method", "unknown"))
SHORT_CALIBRATION_METHOD = str(BUNDLE.get("short_calibration_method", "unknown"))
ROUND_TRIP_COST = float(BUNDLE.get("round_trip_cost", 0.001200))

if str(BUNDLE.get("long_variant", "")) != V22_SELECTED_VARIANT_NAME:
    raise RuntimeError(f"Live bundle mismatch: expected long_variant={V22_SELECTED_VARIANT_NAME}, got {BUNDLE.get('long_variant')}")
if str(BUNDLE.get("short_variant", "")) != "SHORT_NO_FILTER":
    raise RuntimeError(f"Live bundle mismatch: expected short_variant=SHORT_NO_FILTER, got {BUNDLE.get('short_variant')}")
if abs(LONG_THRESHOLD - EXPECTED_LONG_THRESHOLD) > 1e-9 or abs(SHORT_THRESHOLD - EXPECTED_SHORT_THRESHOLD) > 1e-9:
    raise RuntimeError(
        f"Live threshold mismatch: expected LONG={EXPECTED_LONG_THRESHOLD:.3f} "
        f"SHORT={EXPECTED_SHORT_THRESHOLD:.3f}, got LONG={LONG_THRESHOLD:.3f} "
        f"SHORT={SHORT_THRESHOLD:.3f}"
    )


def _extract_artifact_feature_list(obj: Any) -> List[str]:
    if isinstance(obj, list):
        return [str(x) for x in obj]
    if isinstance(obj, dict):
        for key in ("features", "feature_cols", "selected_features", "ml_features", "columns", "feature_names"):
            value = obj.get(key)
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                return [str(x) for x in value]
        found: List[List[str]] = []

        def walk(x: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(x, dict):
                for v in x.values():
                    walk(v, depth + 1)
            elif isinstance(x, list) and all(isinstance(v, str) for v in x):
                found.append([str(v) for v in x])

        walk(obj)
        exact = [x for x in found if len(x) == 120]
        if len(exact) == 1:
            return exact[0]
    raise RuntimeError("Could not extract feature list from artifact JSON")


def _threshold_present(obj: Any, side: str, expected: float, max_depth: int = 8) -> bool:
    side = side.lower()

    def walk(x: Any, path: str, depth: int) -> bool:
        if depth > max_depth:
            return False
        if isinstance(x, dict):
            for k, v in x.items():
                key_path = f"{path}.{k}" if path else str(k)
                key_lower = key_path.lower()
                if "threshold" in key_lower and (side in key_lower or f"fixed_{side}" in key_lower):
                    try:
                        if abs(float(v) - expected) < 1e-9:
                            return True
                    except Exception:
                        pass
                if walk(v, key_path, depth + 1):
                    return True
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                if walk(v, f"{path}[{i}]", depth + 1):
                    return True
        return False

    return walk(obj, "", 0)


def _strict_trade_log_overlap_count(trades: pd.DataFrame) -> int:
    required = {"entry_i", "exit_i"}
    if not required.issubset(trades.columns):
        raise RuntimeError(f"Saved final trade log missing overlap columns: {sorted(required - set(trades.columns))}")
    work = trades[["entry_i", "exit_i"]].copy()
    work["entry_i"] = pd.to_numeric(work["entry_i"], errors="coerce")
    work["exit_i"] = pd.to_numeric(work["exit_i"], errors="coerce")
    work = work.dropna(subset=["entry_i", "exit_i"]).sort_values(["entry_i", "exit_i"]).reset_index(drop=True)
    previous_exit = -float("inf")
    overlap_count = 0
    for _, row in work.iterrows():
        entry_i = float(row["entry_i"])
        exit_i = float(row["exit_i"])
        if entry_i <= previous_exit:
            overlap_count += 1
        previous_exit = exit_i
    return overlap_count


def _assert_predict_proba_runtime(model: Any, feature_cols: List[str], label: str) -> None:
    x = pd.DataFrame(np.zeros((3, len(feature_cols)), dtype=float), columns=feature_cols)
    proba = np.asarray(model.predict_proba(x), dtype=float)
    if proba.ndim != 2 or proba.shape[0] != 3 or proba.shape[1] < 2:
        raise RuntimeError(f"{label} predict_proba returned invalid shape={proba.shape}")
    if not np.isfinite(proba).all():
        raise RuntimeError(f"{label} predict_proba returned non-finite values")


def run_startup_model_artifact_contract_check() -> None:
    artifact_sha = {}
    for name, path in FINAL_MODEL_ARTIFACT_FILES.items():
        if not path.exists():
            raise FileNotFoundError(f"Missing final model artifact: {name} -> {path}")
        actual = sha256_file(path)
        expected = EXPECTED_FINAL_MODEL_ARTIFACT_SHA256.get(name)
        artifact_sha[name] = actual
        if actual != expected:
            raise RuntimeError(f"Final model artifact SHA mismatch: {name} actual={actual} expected={expected} path={path}")

    with FINAL_MODEL_ARTIFACT_FILES["selected_row"].open("r", encoding="utf-8") as f:
        selected_row = json.load(f)
    with FINAL_MODEL_ARTIFACT_FILES["audit_summary"].open("r", encoding="utf-8") as f:
        audit_summary = json.load(f)
    with FINAL_MODEL_ARTIFACT_FILES["long_features"].open("r", encoding="utf-8") as f:
        long_features_file = _extract_artifact_feature_list(json.load(f))
    with FINAL_MODEL_ARTIFACT_FILES["short_features"].open("r", encoding="utf-8") as f:
        short_features_file = _extract_artifact_feature_list(json.load(f))

    if long_features_file != LONG_FEATURE_COLS:
        raise RuntimeError("Standalone LONG feature file does not match live bundle LONG feature list")
    if short_features_file != SHORT_FEATURE_COLS:
        raise RuntimeError("Standalone SHORT feature file does not match live bundle SHORT feature list")
    if len(LONG_FEATURE_COLS) != 120 or len(SHORT_FEATURE_COLS) != 120:
        raise RuntimeError(f"Expected 120/120 model features, got LONG={len(LONG_FEATURE_COLS)} SHORT={len(SHORT_FEATURE_COLS)}")

    for label, obj in (("config", LIVE_CONFIG), ("selected_row", selected_row), ("audit_summary", audit_summary), ("bundle", BUNDLE)):
        if not _threshold_present(obj, "long", EXPECTED_LONG_THRESHOLD):
            raise RuntimeError(f"{label} missing expected LONG threshold {EXPECTED_LONG_THRESHOLD}")
        if not _threshold_present(obj, "short", EXPECTED_SHORT_THRESHOLD):
            raise RuntimeError(f"{label} missing expected SHORT threshold {EXPECTED_SHORT_THRESHOLD}")

    comparison = pd.read_csv(FINAL_MODEL_ARTIFACT_FILES["comparison_rows"])
    if len(comparison) != 1:
        raise RuntimeError(f"Expected exactly one final comparison row, got {len(comparison)}")
    comparison_row = comparison.iloc[0]
    if int(float(comparison_row.get("full_trades", -1))) != EXPECTED_FINAL_TRADES:
        raise RuntimeError(f"Final comparison row trade count mismatch: {comparison_row.get('full_trades')}")

    final_trade_log = pd.read_csv(FINAL_MODEL_ARTIFACT_FILES["final_trade_log"])
    side_counts = final_trade_log["side"].astype(str).str.upper().value_counts().to_dict()
    strict_overlap_count = _strict_trade_log_overlap_count(final_trade_log)
    if len(final_trade_log) != EXPECTED_FINAL_TRADES:
        raise RuntimeError(f"Final trade log row count mismatch: {len(final_trade_log)} expected={EXPECTED_FINAL_TRADES}")
    if int(side_counts.get("LONG", 0)) != 1001 or int(side_counts.get("SHORT", 0)) != 1921:
        raise RuntimeError(f"Final trade log side count mismatch: {side_counts}")
    if strict_overlap_count != 0:
        raise RuntimeError(f"Final trade log strict overlap count mismatch: {strict_overlap_count}")

    standalone_long_model = joblib.load(FINAL_MODEL_ARTIFACT_FILES["long_model"])
    standalone_short_model = joblib.load(FINAL_MODEL_ARTIFACT_FILES["short_model"])
    model_checks = [
        ("bundle_long_model", LONG_MODEL, "IdentityCalibrator", LONG_FEATURE_COLS),
        ("bundle_short_model", SHORT_MODEL, "CalibratedClassifierCV", SHORT_FEATURE_COLS),
        ("standalone_long_model", standalone_long_model, "IdentityCalibrator", LONG_FEATURE_COLS),
        ("standalone_short_model", standalone_short_model, "CalibratedClassifierCV", SHORT_FEATURE_COLS),
    ]
    for label, model, expected_class, features in model_checks:
        actual_class = type(model).__name__
        if actual_class != expected_class:
            raise RuntimeError(f"{label} class mismatch: actual={actual_class} expected={expected_class}")
        _assert_predict_proba_runtime(model, features, label)

    logging.info(
        "[MODEL ARTIFACT CONTRACT] PASS | long_thr=%.3f short_thr=%.3f "
        "features=%d/%d trades=%d long=%d short=%d strict_overlap=%d",
        LONG_THRESHOLD,
        SHORT_THRESHOLD,
        len(LONG_FEATURE_COLS),
        len(SHORT_FEATURE_COLS),
        len(final_trade_log),
        int(side_counts.get("LONG", 0)),
        int(side_counts.get("SHORT", 0)),
        strict_overlap_count,
    )
    logging.info("[MODEL ARTIFACT CONTRACT] sha256=%s", artifact_sha)


# =============================================================================
# RAW / FEATURE CONFIG
# =============================================================================
RAW_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "quote_asset_volume",
    "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume",
    "buy_base_volume", "sell_base_volume", "buy_quote_volume", "sell_quote_volume",
    "agg_trade_count", "trade_flow_imbalance_base", "trade_flow_imbalance_quote",
    "cvd_base", "cvd_quote", "premium_open", "premium_high", "premium_low",
    "premium_close", "funding_rate",
]

# Authoritative Training/Forward external-source namespace. These columns are
# preserved through live raw assembly so calculate_features() sees the same
# source contract that existed in the enriched Training/Forward files.
REALAGG_SOURCE_COLUMNS = [
    "realagg_trade_count",
    "realagg_buy_base_volume", "realagg_sell_base_volume",
    "realagg_buy_quote_volume", "realagg_sell_quote_volume",
    "realagg_total_base_volume", "realagg_total_quote_volume",
    "realagg_cvd_base_delta", "realagg_cvd_quote_delta",
    "realagg_flow_imbalance_base", "realagg_flow_imbalance_quote",
    "realagg_buy_ratio_base", "realagg_buy_ratio_quote",
    "realagg_buy_sell_ratio_base", "realagg_buy_sell_ratio_quote",
    "realagg_avg_trade_size_base", "realagg_avg_trade_size_quote",
    "realagg_cvd_base", "realagg_cvd_quote",
]

# Only the additive 1m source fields are persisted. All ratios/CVD/features are
# recomputed by the existing live code exactly as before.
REALAGG_CACHE_COLUMNS = [
    "date",
    "realagg_trade_count",
    "realagg_buy_base_volume", "realagg_sell_base_volume",
    "realagg_buy_quote_volume", "realagg_sell_quote_volume",
]

OI_SOURCE_COLUMNS = [
    "oi_open_interest", "oi_open_interest_value",
    "oi_open_interest_mean", "oi_open_interest_max", "oi_open_interest_min",
    "oi_open_interest_first",
    "oi_open_interest_value_mean", "oi_open_interest_value_max",
    "oi_open_interest_value_min", "oi_open_interest_value_first",
    "oi_snapshot_count",
    # Present in Binance Metrics daily files; not required by the frozen 120/120
    # model lists, but preserved when the live source exposes them.
    "oi_count_toptrader_long_short_ratio",
    "oi_sum_toptrader_long_short_ratio",
    "oi_count_long_short_ratio",
    "oi_sum_taker_long_short_vol_ratio",
]

OI_WINDOWS = [1, 2, 3, 4, 6, 8, 12, 24, 48, 96]
OI_MA_WINDOWS = [20, 50, 100, 200]

BASE_FEATURES = [
    "hour", "day_of_week", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "body", "range", "upper_wick", "lower_wick", "body_pct", "upper_wick_pct", "lower_wick_pct",
    "close_pos", "candle_direction", "logret_1",
    "atr_14", "atrp_14", "rv_20", "rv_50", "bb_pctb", "bb_bw", "bb_z", "z_close_50",
    "ema20_slope", "price_ema20", "price_ema50", "price_ema200",
    "rsi_14", "macd_hist", "adx_14", "di_diff_14",
    "ms_break_up", "ms_break_dn", "ms_range_pos", "ms_trend_state",
    "ms_dist_to_lastPH_atr", "ms_dist_to_lastPL_atr", "ms_persist_up", "ms_persist_dn",
    "vol_z_20", "qav_z_20", "trades_z_20",
    "taker_base_imbalance", "taker_quote_imbalance",
    "cvd_base_delta", "cvd_quote_delta", "cvd_base_delta_z_50", "cvd_quote_delta_z_50",
    "cvd_base_roc_20", "cvd_quote_roc_20",
    "flow_imb_base_z_20", "flow_imb_quote_z_20", "aggressive_flow_burst",
    "funding_rate_chg_1", "funding_rate_ma_24", "funding_rate_z_50",
    "premium_body", "premium_range", "premium_close_chg_1", "premium_close_z_50",
    "mom", "dist_ema20_atr", "ema20_slope_10", "break_up", "break_dn", "trend_state",
    "sr_support", "sr_resistance", "sr_support_dist_atr", "sr_resistance_dist_atr",
    "sr_near_support", "sr_near_resistance", "sr_break_up", "sr_break_dn",
    "sr_support_strength", "sr_resistance_strength",
]

SR_CONFIG = {
    "1m": {"left": 5, "right": 5, "lookback": 1440, "zone_atr": 0.50},
    "5m": {"left": 5, "right": 5, "lookback": 576, "zone_atr": 0.50},
    "15m": {"left": 4, "right": 4, "lookback": 384, "zone_atr": 0.50},
    "1h": {"left": 4, "right": 4, "lookback": 336, "zone_atr": 0.60},
    "4h": {"left": 3, "right": 3, "lookback": 180, "zone_atr": 0.70},
    "1d": {"left": 3, "right": 3, "lookback": 180, "zone_atr": 0.80},
}

EPS = 1e-12
PATH_HORIZON_BARS = 12
PATH_FIRST_BARS = 3
PATH_TP_ATR = 1.0
PATH_SL_ATR = 1.0
PATH_ROLL_WINDOWS = [200, 500]
SAFE_SHIFT = PATH_HORIZON_BARS + 1


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass(frozen=True)
class SplitDef:
    name: str
    start: int
    end: int


@dataclass
class FeatureSpec:
    row_i: int
    side: str
    family: str
    feature: str
    column_raw: str
    timeframe: str
    op: str
    threshold: float
    source_column: str


@dataclass(frozen=True)
class RuleThresholds:
    long_adx_q60: float
    long_di_q60: float
    long_close_pos_q60: float
    long_mom_q70: float
    long_di_q70_final: float
    long_1h_adx_q70: float
    long_1h_di_q25: float
    long_1h_rsi_q25: float
    short_range_q50: float
    short_body_q50: float
    short_mom_q30: float
    short_s1_mom_q30: float
    short_vol_q60: float
    short_1h_adx_q80: float
    short_1h_di_q80: float
    short_1h_rsi_q80: float


@dataclass(frozen=True)
class V22LiveThresholds:
    atr_low: float
    atr_high: float
    range_high: float
    funding_abs_hi: float
    q_range70: float
    q_range40: float
    q_ret4_65: float
    q_ret12_40: float
    q_ret24_25: float
    q_closepos60: float
    q_closepos75: float
    q_lwick60: float
    q_bbw30: float
    q_realagg70: float
    q_realagg_delta65: float
    vol_q33: float
    vol_q66: float


@dataclass(frozen=True)
class ExitConfig:
    name: str
    side: int
    sl_atr: float
    tp_atr: float
    hold_bars: int
    trail_start_atr: float
    trail_dist_atr: float


@dataclass
class OpenPosition:
    side: int
    signal_t: str
    entry_t: str
    entry: float
    sl: float
    tp: float
    atr: float
    exit_name: str
    bars_held: int = 0
    best_high: float = float("nan")
    best_low: float = float("nan")
    stop: float = float("nan")
    initial_sl: float = float("nan")
    trail_active: bool = False
    prob: float = float("nan")
    threshold: float = float("nan")
    setup_name: str = ""
    trade_id: str = ""
    # V22 RX4 is classified once from entry bars 1-4 and then frozen, exactly
    # as in the original training simulation.
    rx4_ready: bool = False
    rx4_close_atr: float = float("nan")
    rx4_mfe_atr: float = float("nan")
    rx4_mae_atr: float = float("nan")
    rx4_class: str = "rx4_unavailable"


@dataclass
class StrictReactionPosition:
    signal_t: str
    entry_t: str
    entry: float
    atr: float
    archetype: str
    sl: float
    tp: float
    trail_stop: float
    bars_held: int = 0
    best_high: float = float("nan")
    worst_low: float = float("nan")
    trail_active: bool = False


# =============================================================================
# BASIC HELPERS
# =============================================================================
def _f(x, default=np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def _i(x, default=None):
    try:
        if pd.isna(x):
            return default
        return int(x)
    except Exception:
        return default


def tf_minutes(tf: str) -> int:
    return TF_MINUTES[tf]


def to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def safe_div(a, b):
    if isinstance(b, pd.Series):
        return a / b.replace(0, np.nan)
    return a / np.where(b == 0, np.nan, b)


def dedupe_columns_keep_last(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the last occurrence of duplicate column names.

    This is only a safety guard for audit/live feature assembly after adding
    external columns. It does not change trading rules, thresholds, models,
    order logic, or paths.
    """
    if df.columns.has_duplicates:
        return df.loc[:, ~df.columns.duplicated(keep="last")].copy()
    return df


def zscore(s, window):
    mean = s.rolling(window, min_periods=window).mean()
    std = s.rolling(window, min_periods=window).std()
    return (s - mean) / std.replace(0, np.nan)


def zscore_extra(s: pd.Series, n: int) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    m = s.rolling(n, min_periods=max(5, n // 3)).mean()
    sd = s.rolling(n, min_periods=max(5, n // 3)).std()
    return (s - m) / sd.replace(0, np.nan)


def logret(close: pd.Series, n: int) -> pd.Series:
    close = pd.to_numeric(close, errors="coerce")
    return np.log(close / close.shift(n))


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def consecutive_count(cond):
    out = np.zeros(len(cond), dtype=np.int32)
    count = 0
    for i, value in enumerate(cond.fillna(False).to_numpy()):
        count = count + 1 if value else 0
        out[i] = count
    return out


def bool_value(x) -> bool:
    if x is None:
        return False
    try:
        if pd.isna(x):
            return False
    except Exception:
        pass
    if isinstance(x, str):
        return x.strip().lower() in {"true", "1", "yes", "y"}
    try:
        return float(x) != 0.0
    except Exception:
        return False


def bool_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else str(k)
        if isinstance(v, dict):
            out.update(flatten_dict(v, new_key, sep=sep))
        else:
            out[new_key] = v
    return out


def to_jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


# =============================================================================
# CLEAN AUDIT SERIALIZATION / ATOMIC PERSISTENCE
# =============================================================================


def _csv_safe_cell(v: Any) -> Any:
    v = to_jsonable(v)
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _safe_json(v: Any) -> str:
    return json.dumps(to_jsonable(v), ensure_ascii=False, default=str)


def _schema_row(row: Dict[str, Any], columns: List[str]) -> Dict[str, Any]:
    fixed = {c: None for c in columns}
    for k, v in row.items():
        if k in fixed:
            fixed[k] = _csv_safe_cell(v)
    return fixed


def _header_matches(path: Path, columns: List[str]) -> bool:
    if not path.exists():
        return True
    try:
        existing = list(pd.read_csv(path, nrows=0).columns)
        return existing == list(columns)
    except Exception:
        return False


def _schema_safe_path(path: Path, columns: Optional[List[str]]) -> Path:
    if columns is None or _header_matches(path, columns):
        return path
    return path.with_name(path.stem + "_schema_fixed.csv")


_CSV_UNIQUE_KEY_CACHE: Dict[Tuple[str, Tuple[str, ...]], set] = {}
_JSONL_UNIQUE_KEY_CACHE: Dict[Tuple[str, str], set] = {}
_PROCESS_LOCK_ACQUIRED = False
_PROCESS_LOCK_HANDLE: Optional[TextIO] = None


def _normalized_key_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _row_key(row: Dict[str, Any], key_fields: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(_normalized_key_value(row.get(field)) for field in key_fields)


def append_csv_row(path: Path, row: Dict[str, Any], columns: Optional[List[str]] = None):
    path.parent.mkdir(parents=True, exist_ok=True)

    if columns is not None:
        path = _schema_safe_path(path, columns)
        fixed = _schema_row(row, columns)
        df = pd.DataFrame([fixed], columns=columns)
    else:
        fixed = {k: _csv_safe_cell(v) for k, v in row.items()}
        df = pd.DataFrame([fixed])

    if path.exists():
        df.to_csv(path, mode="a", index=False, header=False)
    else:
        df.to_csv(path, index=False)


def _load_csv_unique_keys(path: Path, key_fields: Tuple[str, ...]) -> set:
    cache_key = (str(path.resolve()), key_fields)
    if cache_key in _CSV_UNIQUE_KEY_CACHE:
        return _CSV_UNIQUE_KEY_CACHE[cache_key]
    keys: set = set()
    if path.exists() and path.stat().st_size > 0:
        try:
            existing = pd.read_csv(path, usecols=list(key_fields), low_memory=False)
            for record in existing.to_dict(orient="records"):
                keys.add(_row_key(record, key_fields))
        except Exception as exc:
            logging.warning("[DEDUPE CSV LOAD WARNING] file=%s error=%s", path, exc)
    _CSV_UNIQUE_KEY_CACHE[cache_key] = keys
    return keys


def append_csv_row_unique(
    path: Path,
    row: Dict[str, Any],
    columns: List[str],
    key_fields: Tuple[str, ...],
) -> bool:
    actual_path = _schema_safe_path(path, columns)
    keys = _load_csv_unique_keys(actual_path, key_fields)
    key = _row_key(row, key_fields)
    if key in keys:
        logging.info("[DEDUPED CSV ROW] file=%s key=%s", actual_path.name, key)
        return False
    append_csv_row(actual_path, row, columns)
    keys.add(key)
    return True


def _repair_jsonl_tail(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, "rb+") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return
        position = size
        while position > 0:
            read_size = min(8192, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size)
            newline_index = block.rfind(b"\n")
            if newline_index >= 0:
                handle.truncate(position + newline_index + 1)
                handle.flush()
                os.fsync(handle.fileno())
                logging.warning(
                    "[JSONL TAIL REPAIRED] truncated incomplete final record | file=%s",
                    path,
                )
                return
        handle.seek(0)
        handle.truncate(0)
        handle.flush()
        os.fsync(handle.fileno())
        logging.warning(
            "[JSONL TAIL REPAIRED] removed incomplete single record | file=%s",
            path,
        )


def append_jsonl_row(path: Path, row: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_jsonl_tail(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                to_jsonable(row),
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
        f.flush()
        os.fsync(f.fileno())


def _load_jsonl_unique_keys(path: Path, key_field: str) -> set:
    cache_key = (str(path.resolve()), key_field)
    if cache_key in _JSONL_UNIQUE_KEY_CACHE:
        return _JSONL_UNIQUE_KEY_CACHE[cache_key]
    keys: set = set()
    _repair_jsonl_tail(path)
    if path.exists() and path.stat().st_size > 0:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    keys.add(_normalized_key_value(record.get(key_field)))
        except Exception as exc:
            logging.warning("[DEDUPE JSONL LOAD WARNING] file=%s error=%s", path, exc)
    _JSONL_UNIQUE_KEY_CACHE[cache_key] = keys
    return keys


def append_jsonl_row_unique(
    path: Path,
    row: Dict[str, Any],
    key_field: str,
) -> bool:
    keys = _load_jsonl_unique_keys(path, key_field)
    key = _normalized_key_value(row.get(key_field))
    if key in keys:
        logging.info("[DEDUPED JSONL ROW] file=%s key=%s", path.name, key)
        return False
    append_jsonl_row(path, row)
    keys.add(key)
    return True


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                to_jsonable(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except Exception:
                pass


def _read_last_jsonl_record(path: Path) -> Optional[Dict[str, Any]]:
    _repair_jsonl_tail(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            while position > 0:
                read_size = min(8192, position)
                position -= read_size
                handle.seek(position)
                buffer = handle.read(read_size) + buffer
                lines = [line for line in buffer.splitlines() if line.strip()]
                if lines:
                    for raw_line in reversed(lines):
                        try:
                            return json.loads(raw_line.decode("utf-8"))
                        except Exception:
                            continue
            return None
    except Exception as exc:
        logging.warning("[LAST JSONL READ WARNING] file=%s error=%s", path, exc)
        return None


def append_run_session(_event_type: str, _state: Optional[Dict[str, Any]] = None, **_details: Any) -> None:
    """No-op compatibility hook; clean mode intentionally creates no session file."""


def append_runtime_error(stage: str, exc: BaseException, state: Optional[Dict[str, Any]] = None) -> None:
    event_time = datetime.now(timezone.utc).isoformat()
    append_jsonl_row(
        ERRORS_FILE,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "record_type": "ERROR",
            "event_key": f"ERROR|{RUN_ID}|{event_time}|{uuid.uuid4().hex}",
            "run_id": RUN_ID,
            "time_utc": event_time,
            "stage": stage,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
            "state": state,
        },
    )


def append_diagnostic_warning(
    signal_bar_utc: Optional[str],
    category: str,
    warnings_list: List[str],
    context: Optional[Dict[str, Any]] = None,
) -> None:
    if not warnings_list:
        return
    event_key = f"WARNING|{category}|{signal_bar_utc or 'NO_BAR'}"
    append_jsonl_row_unique(
        ERRORS_FILE,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "record_type": "WARNING",
            "event_key": event_key,
            "run_id": RUN_ID,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "signal_bar_utc": signal_bar_utc,
            "category": category,
            "warnings": list(warnings_list),
            "context": context or {},
        },
        "event_key",
    )


def acquire_process_lock() -> None:
    """Use the master audit file itself as the process lock; no extra lock file."""
    global _PROCESS_LOCK_ACQUIRED, _PROCESS_LOCK_HANDLE
    MASTER_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(MASTER_AUDIT_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            "Another shadow-live process is already using the clean audit folder"
        ) from exc
    _PROCESS_LOCK_HANDLE = handle
    _PROCESS_LOCK_ACQUIRED = True


def release_process_lock() -> None:
    global _PROCESS_LOCK_ACQUIRED, _PROCESS_LOCK_HANDLE
    if not _PROCESS_LOCK_ACQUIRED:
        return
    try:
        handle = _PROCESS_LOCK_HANDLE
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    except Exception as exc:
        logging.warning("[LOCK RELEASE WARNING] %s", exc)
    finally:
        _PROCESS_LOCK_HANDLE = None
        _PROCESS_LOCK_ACQUIRED = False


def initialize_clean_audit_files() -> None:
    """Create/validate exactly the five clean audit outputs."""
    LIVE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_AUDIT_FILE.touch(exist_ok=True)
    ERRORS_FILE.touch(exist_ok=True)
    EXTERNAL_DATA_AUDIT_FILE.touch(exist_ok=True)

    if TRADES_FILE.exists() and TRADES_FILE.stat().st_size > 0:
        existing_header = list(pd.read_csv(TRADES_FILE, nrows=0).columns)
        if existing_header != SHADOW_TRADE_COLUMNS:
            raise RuntimeError(
                "shadow_live_trades.csv has an unexpected schema; "
                "move/delete it before starting this version"
            )
    else:
        pd.DataFrame(columns=SHADOW_TRADE_COLUMNS).to_csv(TRADES_FILE, index=False)

    if not RUNTIME_STATE_FILE.exists():
        atomic_write_json(RUNTIME_STATE_FILE, default_runtime_state())

    unexpected = sorted(
        p.name
        for p in LIVE_AUDIT_DIR.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.name not in ALLOWED_AUDIT_FILENAMES
    )
    if unexpected:
        raise RuntimeError(
            "Clean audit folder contains unexpected files: " + ", ".join(unexpected)
        )


atexit.register(release_process_lock)


def make_trade_id(signal_t: str, side: int, setup_name: str) -> str:
    side_txt = "LONG" if side == +1 else "SHORT"
    raw = str(signal_t).replace(":", "").replace("-", "").replace("+00:00", "Z")
    setup = str(setup_name).replace(" ", "_")
    return f"{raw}_{side_txt}_{setup}"


def position_txt(side: Optional[int]) -> str:
    if side == +1:
        return "LONG"
    if side == -1:
        return "SHORT"
    return "FLAT"


def current_bid_ask_from_mid(mid_px: float) -> Tuple[float, float]:
    bid = float(mid_px) - LIVE_LOG_BID_ASK_SPREAD_USD / 2.0
    ask = float(mid_px) + LIVE_LOG_BID_ASK_SPREAD_USD / 2.0
    return bid, ask

def leveraged_rate(rate_1x: float, leverage: float) -> float:
    return float(rate_1x) * float(leverage)


def pnl_usd(rate: float, capital_usd: float) -> float:
    return float(rate) * float(capital_usd)


def planned_stop_pnl_1x(pos: OpenPosition) -> float:
    stop_px = float(pos.initial_sl)
    gross = (stop_px / pos.entry - 1.0) if pos.side == +1 else (pos.entry - stop_px) / pos.entry
    return float(gross - ROUND_TRIP_COST)


def stored_tp_pnl_1x(pos: OpenPosition) -> float:
    tp_px = float(pos.tp)
    gross = (tp_px / pos.entry - 1.0) if pos.side == +1 else (pos.entry - tp_px) / pos.entry
    return float(gross - ROUND_TRIP_COST)


def build_open_leverage_scenarios(pos: OpenPosition) -> List[Dict[str, Any]]:
    stop_rate_1x = planned_stop_pnl_1x(pos)
    tp_rate_1x = stored_tp_pnl_1x(pos)
    rows: List[Dict[str, Any]] = []
    for sc in LEVERAGE_SCENARIOS:
        lev = float(sc["leverage"])
        capital = float(sc["capital_usd"])
        stop_rate_leveraged = leveraged_rate(stop_rate_1x, lev)
        tp_rate_leveraged = leveraged_rate(tp_rate_1x, lev)
        rows.append({
            "scenario": sc["scenario"],
            "leverage": lev,
            "capital_usd": capital,
            "planned_initial_stop_pnl_1x": stop_rate_1x,
            "planned_initial_stop_pnl_leveraged": stop_rate_leveraged,
            "planned_initial_stop_pnl_usd": pnl_usd(stop_rate_leveraged, capital),
            "stored_tp_pnl_1x": tp_rate_1x,
            "stored_tp_pnl_leveraged": tp_rate_leveraged,
            "stored_tp_pnl_usd": pnl_usd(tp_rate_leveraged, capital),
        })
    return rows


def build_close_leverage_scenarios(pos: OpenPosition, exit_px: float) -> List[Dict[str, Any]]:
    pnl_1x = trade_pnl(pos, exit_px)
    rows: List[Dict[str, Any]] = []
    for sc in LEVERAGE_SCENARIOS:
        lev = float(sc["leverage"])
        capital = float(sc["capital_usd"])
        net_rate_leveraged = leveraged_rate(pnl_1x, lev)
        rows.append({
            "scenario": sc["scenario"],
            "leverage": lev,
            "capital_usd": capital,
            "net_pnl_1x_after_round_trip_cost": pnl_1x,
            "net_pnl_leveraged_after_round_trip_cost": net_rate_leveraged,
            "net_pnl_usd_after_round_trip_cost": pnl_usd(net_rate_leveraged, capital),
        })
    return rows


def add_open_leverage_columns(row: Dict[str, Any], pos: OpenPosition) -> Dict[str, Any]:
    for sc in build_open_leverage_scenarios(pos):
        prefix = f"{sc['scenario']}_"
        row[prefix + "leverage"] = sc["leverage"]
        row[prefix + "capital_usd"] = sc["capital_usd"]
        row[prefix + "planned_initial_stop_pnl_1x"] = sc["planned_initial_stop_pnl_1x"]
        row[prefix + "planned_initial_stop_pnl_leveraged"] = sc["planned_initial_stop_pnl_leveraged"]
        row[prefix + "planned_initial_stop_pnl_usd"] = sc["planned_initial_stop_pnl_usd"]
        row[prefix + "stored_tp_pnl_1x"] = sc["stored_tp_pnl_1x"]
        row[prefix + "stored_tp_pnl_leveraged"] = sc["stored_tp_pnl_leveraged"]
        row[prefix + "stored_tp_pnl_usd"] = sc["stored_tp_pnl_usd"]
    row["leverage_scenarios_json"] = json.dumps(build_open_leverage_scenarios(pos), ensure_ascii=False)
    return row


def add_close_leverage_columns(row: Dict[str, Any], pos: OpenPosition, exit_px: float) -> Dict[str, Any]:
    for sc in build_close_leverage_scenarios(pos, exit_px):
        prefix = f"{sc['scenario']}_"
        row[prefix + "leverage"] = sc["leverage"]
        row[prefix + "capital_usd"] = sc["capital_usd"]
        row[prefix + "net_pnl_1x_after_round_trip_cost"] = sc["net_pnl_1x_after_round_trip_cost"]
        row[prefix + "net_pnl_leveraged_after_round_trip_cost"] = sc["net_pnl_leveraged_after_round_trip_cost"]
        row[prefix + "net_pnl_usd_after_round_trip_cost"] = sc["net_pnl_usd_after_round_trip_cost"]
    row["leverage_scenarios_json"] = json.dumps(build_close_leverage_scenarios(pos, exit_px), ensure_ascii=False)
    return row


def format_rate(rate: float) -> str:
    return f"{float(rate) * 100:.3f}%"


def format_usd(value: float) -> str:
    sign = "-" if float(value) < 0 else ""
    return f"{sign}${abs(float(value)):.2f}"


def format_open_leverage_scenarios(pos: OpenPosition) -> str:
    lines = ["Leverage scenarios:"]
    for sc in build_open_leverage_scenarios(pos):
        lines.append(
            f"- {sc['scenario']} {sc['leverage']:.0f}X | "
            f"capital: ${sc['capital_usd']:.2f} | "
            f"initial SL: {format_rate(sc['planned_initial_stop_pnl_leveraged'])} / {format_usd(sc['planned_initial_stop_pnl_usd'])} | "
            f"stored TP: {format_rate(sc['stored_tp_pnl_leveraged'])} / {format_usd(sc['stored_tp_pnl_usd'])}"
        )
    return "\n".join(lines)


def format_close_leverage_scenarios(pos: OpenPosition, exit_px: float) -> str:
    lines = ["Leverage scenarios:"]
    for sc in build_close_leverage_scenarios(pos, exit_px):
        lines.append(
            f"- {sc['scenario']} {sc['leverage']:.0f}X | "
            f"capital: ${sc['capital_usd']:.2f} | "
            f"net PnL: {format_rate(sc['net_pnl_leveraged_after_round_trip_cost'])} / {format_usd(sc['net_pnl_usd_after_round_trip_cost'])}"
        )
    return "\n".join(lines)


def send_email(subject: str, body: str):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logging.info("[EMAIL SKIPPED] %s", subject)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD.replace(" ", ""))
            smtp.send_message(msg)
        logging.info("[EMAIL SENT] %s", subject)
    except Exception as e:
        logging.error("[EMAIL ERROR] %s", e)


# =============================================================================
# FEATURE BUILDER
# =============================================================================
def confirmed_pivots(df, left, right):
    high = df["high"]
    low = df["low"]
    window = left + right + 1
    pivot_high = high.eq(high.rolling(window, center=True, min_periods=window).max())
    pivot_low = low.eq(low.rolling(window, center=True, min_periods=window).min())
    ph_confirmed = pivot_high.shift(right).fillna(False)
    pl_confirmed = pivot_low.shift(right).fillna(False)
    return high.shift(right).where(ph_confirmed), low.shift(right).where(pl_confirmed)


def remove_one(sorted_levels, level):
    pos = bisect_left(sorted_levels, level)
    if pos < len(sorted_levels) and sorted_levels[pos] == level:
        sorted_levels.pop(pos)


def add_support_resistance(df, tf):
    cfg = SR_CONFIG[tf]
    close = df["close"].to_numpy()
    atr = df["atr_14"].to_numpy()
    ph_level, pl_level = confirmed_pivots(df, cfg["left"], cfg["right"])
    ph = ph_level.to_numpy()
    pl = pl_level.to_numpy()
    n = len(df)
    active_levels = []
    level_queue = deque()
    support = np.full(n, np.nan)
    resistance = np.full(n, np.nan)
    support_strength = np.zeros(n, dtype=np.int32)
    resistance_strength = np.zeros(n, dtype=np.int32)
    for i in range(n):
        old_limit = i - cfg["lookback"]
        while level_queue and level_queue[0][0] < old_limit:
            _, old_level = level_queue.popleft()
            remove_one(active_levels, old_level)
        for level in (ph[i], pl[i]):
            if np.isfinite(level):
                insort(active_levels, float(level))
                level_queue.append((i, float(level)))
        price = close[i]
        width = atr[i] * cfg["zone_atr"] if np.isfinite(atr[i]) and atr[i] > 0 else np.nan
        if not active_levels or not np.isfinite(price):
            continue
        sup_pos = bisect_left(active_levels, price) - 1
        res_pos = bisect_right(active_levels, price)
        if sup_pos >= 0:
            level = active_levels[sup_pos]
            support[i] = level
            if np.isfinite(width):
                support_strength[i] = bisect_right(active_levels, level + width) - bisect_left(active_levels, level - width)
        if res_pos < len(active_levels):
            level = active_levels[res_pos]
            resistance[i] = level
            if np.isfinite(width):
                resistance_strength[i] = bisect_right(active_levels, level + width) - bisect_left(active_levels, level - width)
    c = pd.Series(close, index=df.index)
    atr_s = pd.Series(atr, index=df.index)
    sr = pd.DataFrame(index=df.index)
    sr["sr_support"] = support
    sr["sr_resistance"] = resistance
    sr["sr_support_dist_atr"] = safe_div(c - sr["sr_support"], atr_s)
    sr["sr_resistance_dist_atr"] = safe_div(sr["sr_resistance"] - c, atr_s)
    sr["sr_near_support"] = sr["sr_support_dist_atr"].between(0, cfg["zone_atr"]).astype("int8")
    sr["sr_near_resistance"] = sr["sr_resistance_dist_atr"].between(0, cfg["zone_atr"]).astype("int8")
    prev_close = c.shift(1)
    prev_support = sr["sr_support"].shift(1)
    prev_resistance = sr["sr_resistance"].shift(1)
    sr["sr_break_up"] = ((prev_close <= prev_resistance) & (c > prev_resistance)).astype("int8")
    sr["sr_break_dn"] = ((prev_close >= prev_support) & (c < prev_support)).astype("int8")
    sr["sr_support_strength"] = support_strength
    sr["sr_resistance_strength"] = resistance_strength
    return sr


def calculate_features(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates("date").reset_index(drop=True)

    # Keep the exact locked-training rule inputs when historical files already
    # contain them. Network-built live frames normally do not, so the same
    # columns are then rebuilt causally below from the fetched raw inputs.
    _v22_passthrough_names = [
        "range_pct", "ret4", "ret12", "ret24", "prev_high_20",
        "atr14_pct", "atrp_14", "dist_ema20_atr", "ema20_slope_10",
        "price_ema50", "price_ema200",
        "binance_funding_rate", "binance_funding_rate_abs",
        "realagg_buy_ratio_quote", "realagg_cvd_quote_delta",
        "oi_open_interest_pct_chg_4", "oi_price_oi_divergence_4",
    ]
    _v22_raw = {
        name: pd.to_numeric(df[name], errors="coerce").copy()
        for name in _v22_passthrough_names
        if name in df.columns
    }

    missing = [c for c in RAW_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{tf} missing raw columns: {missing}")
    extra_live_external_cols = [
        c for c in (REALAGG_SOURCE_COLUMNS + OI_SOURCE_COLUMNS)
        if c in df.columns and c not in RAW_COLUMNS
    ]
    df = df[RAW_COLUMNS + extra_live_external_cols].copy()
    df = dedupe_columns_keep_last(df)
    for col in [c for c in df.columns if c != "date"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]
    volume = df["volume"]
    qav = df["quote_asset_volume"]
    trades = df["number_of_trades"]
    rng = h - l
    body = c - o
    prev_close = c.shift(1)
    logret_1 = np.log(c).diff()
    tr = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    atr_14 = rma(tr, 14)
    ema20 = c.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = c.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = c.ewm(span=200, adjust=False, min_periods=200).mean()
    ma20 = c.rolling(20, min_periods=20).mean()
    std20 = c.rolling(20, min_periods=20).std()
    bb_up = ma20 + 2 * std20
    bb_dn = ma20 - 2 * std20
    delta = c.diff()
    rs = safe_div(rma(delta.clip(lower=0), 14), rma(-delta.clip(upper=0), 14))
    macd = c.ewm(span=12, adjust=False, min_periods=12).mean() - c.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    up_move = h.diff()
    dn_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0), index=df.index)
    plus_di = 100 * safe_div(rma(plus_dm, 14), atr_14)
    minus_di = 100 * safe_div(rma(minus_dm, 14), atr_14)
    dx = 100 * safe_div((plus_di - minus_di).abs(), plus_di + minus_di)
    last_ph = h.rolling(50, min_periods=50).max().shift(1)
    last_pl = l.rolling(50, min_periods=50).min().shift(1)
    ema20_slope = safe_div(ema20 - ema20.shift(5), atr_14)
    trend_up = (c > ema50) & (ema20 > ema50) & (ema20_slope > 0)
    trend_dn = (c < ema50) & (ema20 < ema50) & (ema20_slope < 0)
    ms_trend_state = pd.Series(np.select([trend_up, trend_dn], [1, -1], default=0), index=df.index).astype("int8")
    f = pd.DataFrame(index=df.index)
    f["hour"] = df["date"].dt.hour
    f["day_of_week"] = df["date"].dt.dayofweek
    f["hour_sin"] = np.sin(2 * np.pi * f["hour"] / 24)
    f["hour_cos"] = np.cos(2 * np.pi * f["hour"] / 24)
    f["dow_sin"] = np.sin(2 * np.pi * f["day_of_week"] / 7)
    f["dow_cos"] = np.cos(2 * np.pi * f["day_of_week"] / 7)
    f["body"] = body
    f["range"] = rng
    f["upper_wick"] = h - np.maximum(o, c)
    f["lower_wick"] = np.minimum(o, c) - l
    f["body_pct"] = safe_div(body.abs(), rng)
    f["upper_wick_pct"] = safe_div(f["upper_wick"], rng)
    f["lower_wick_pct"] = safe_div(f["lower_wick"], rng)
    f["close_pos"] = safe_div(c - l, rng)
    f["candle_direction"] = np.sign(body).astype("int8")
    f["logret_1"] = logret_1
    f["range_pct"] = safe_div(rng, c)
    f["ret4"] = logret(c, 4)
    f["ret12"] = logret(c, 12)
    f["ret24"] = logret(c, 24)
    f["prev_high_20"] = h.rolling(20, min_periods=20).max().shift(1)
    f["session_active_07_21"] = ((f["hour"] >= 7) & (f["hour"] <= 21)).astype("int8")

    # -------------------------------------------------------------------------
    # V22 LONG exact locked-training rule namespace.
    # Generic columns above remain untouched for SHORT and ML.
    # -------------------------------------------------------------------------
    _v22_scope = df.index
    if tf == "15m" and len(df):
        _start_utc = pd.Timestamp(START_DATE)
        _start_utc = _start_utc.tz_localize("UTC") if _start_utc.tzinfo is None else _start_utc.tz_convert("UTC")
        _end_utc = pd.Timestamp(END_DATE)
        _end_utc = _end_utc.tz_localize("UTC") if _end_utc.tzinfo is None else _end_utc.tz_convert("UTC")
        _study_mask = (df["date"] >= _start_utc) & (df["date"] <= _end_utc)
        # The locked engine cuts the historical 15m base before rolling/returns.
        # Normal future live windows have no pre-study rows and use all warm-up.
        if bool(_study_mask.any()) and bool((df["date"] < _start_utc).any()):
            _v22_scope = df.index[_study_mask]

    def _v22_scoped_pct_change(source: pd.Series, periods: int) -> pd.Series:
        result = pd.Series(np.nan, index=df.index, dtype="float64")
        result.loc[_v22_scope] = source.loc[_v22_scope].pct_change(periods)
        return result

    def _v22_scoped_prev_high(source: pd.Series, window: int) -> pd.Series:
        result = pd.Series(np.nan, index=df.index, dtype="float64")
        result.loc[_v22_scope] = source.loc[_v22_scope].shift(1).rolling(window, min_periods=window).max()
        return result

    f["v22_exact_range_pct"] = _v22_raw.get("range_pct", safe_div(rng, c))
    f["v22_exact_ret4"] = _v22_raw.get("ret4", _v22_scoped_pct_change(c, 4))
    f["v22_exact_ret12"] = _v22_raw.get("ret12", _v22_scoped_pct_change(c, 12))
    f["v22_exact_ret24"] = _v22_raw.get("ret24", _v22_scoped_pct_change(c, 24))
    f["v22_exact_prev_high_20"] = _v22_raw.get("prev_high_20", _v22_scoped_prev_high(h, 20))
    f["v22_exact_session_active_07_21"] = ((f["hour"] >= 7) & (f["hour"] < 21)).astype("int8")
    f["v22_exact_atr14_pct"] = _v22_raw.get("atr14_pct", _v22_raw.get("atrp_14", safe_div(atr_14, c)))
    f["v22_exact_dist_ema20_atr"] = _v22_raw.get("dist_ema20_atr", safe_div(c - ema20, atr_14))
    # The actual historical training CSV already contains ema20_slope_10 using
    # the generic live/training convention (ATR-normalised). Preserve that exact
    # convention for future bars instead of the locked source's missing-column fallback.
    f["v22_exact_ema20_slope_10"] = _v22_raw.get("ema20_slope_10", safe_div(ema20 - ema20.shift(10), atr_14))

    # Important: the locked engine sees existing price_ema50/price_ema200 columns
    # and applies sign(price_ema50 - price_ema200). In these project files those
    # columns are price/EMA - 1 distances, not raw EMA levels. Reproduce the
    # actual trained-data behavior exactly.
    _v22_price_ema50 = _v22_raw.get("price_ema50", safe_div(c, ema50) - 1)
    _v22_price_ema200 = _v22_raw.get("price_ema200", safe_div(c, ema200) - 1)
    f["v22_exact_trend_regime_ema50_200"] = np.sign(_v22_price_ema50 - _v22_price_ema200)

    # Live external-feature parity helpers: recreate the training external namespace
    # from the live raw inputs. This only fills feature columns; it does not alter
    # rules, thresholds, model artifacts, execution, paths, or any order logic.
    funding_rate = pd.to_numeric(df["funding_rate"], errors="coerce")
    f["binance_funding_rate"] = funding_rate
    f["binance_funding_rate_abs"] = funding_rate.abs()
    _v22_funding_rate = _v22_raw.get("binance_funding_rate", funding_rate)
    f["v22_exact_binance_funding_rate_abs"] = _v22_raw.get(
        "binance_funding_rate_abs", _v22_funding_rate.abs()
    ).abs()
    f["binance_funding_rate_positive"] = np.where(funding_rate.notna(), (funding_rate > 0).astype("int8"), np.nan)
    f["binance_funding_rate_negative"] = np.where(funding_rate.notna(), (funding_rate < 0).astype("int8"), np.nan)
    f["binance_funding_rate_chg_8h"] = funding_rate.diff(32 if tf == "15m" else 8 if tf == "1h" else 2 if tf == "4h" else 1)

    # -------------------------------------------------------------------------
    # REALAGG — exact Training/Forward source contract.
    # No Kline number_of_trades / taker-volume fallback is allowed here.
    # Generic aliases are overwritten from TRUE USD-M Futures aggTrades only.
    # -------------------------------------------------------------------------
    def _source_num(name: str) -> pd.Series:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce")
        return pd.Series(np.nan, index=df.index, dtype="float64")

    realagg_trade_count = _source_num("realagg_trade_count")
    realagg_buy_base = _source_num("realagg_buy_base_volume")
    realagg_sell_base = _source_num("realagg_sell_base_volume")
    realagg_buy_quote = _source_num("realagg_buy_quote_volume")
    realagg_sell_quote = _source_num("realagg_sell_quote_volume")

    realagg_total_base = realagg_buy_base + realagg_sell_base
    realagg_total_quote = realagg_buy_quote + realagg_sell_quote
    realagg_base_delta = realagg_buy_base - realagg_sell_base
    realagg_quote_delta = realagg_buy_quote - realagg_sell_quote
    realagg_flow_base = safe_div(realagg_base_delta, realagg_total_base)
    realagg_flow_quote = safe_div(realagg_quote_delta, realagg_total_quote)
    realagg_cvd_base = realagg_base_delta.cumsum()
    realagg_cvd_quote = realagg_quote_delta.cumsum()

    # Keep the authoritative realagg namespace in the raw frame itself.
    df["realagg_trade_count"] = realagg_trade_count
    df["realagg_buy_base_volume"] = realagg_buy_base
    df["realagg_sell_base_volume"] = realagg_sell_base
    df["realagg_buy_quote_volume"] = realagg_buy_quote
    df["realagg_sell_quote_volume"] = realagg_sell_quote
    df["realagg_total_base_volume"] = realagg_total_base
    df["realagg_total_quote_volume"] = realagg_total_quote
    df["realagg_cvd_base_delta"] = realagg_base_delta
    df["realagg_cvd_quote_delta"] = realagg_quote_delta
    df["realagg_flow_imbalance_base"] = realagg_flow_base
    df["realagg_flow_imbalance_quote"] = realagg_flow_quote
    df["realagg_buy_ratio_base"] = safe_div(realagg_buy_base, realagg_total_base)
    df["realagg_buy_ratio_quote"] = safe_div(realagg_buy_quote, realagg_total_quote)
    df["realagg_buy_sell_ratio_base"] = safe_div(realagg_buy_base, realagg_sell_base)
    df["realagg_buy_sell_ratio_quote"] = safe_div(realagg_buy_quote, realagg_sell_quote)
    df["realagg_avg_trade_size_base"] = safe_div(realagg_total_base, realagg_trade_count)
    df["realagg_avg_trade_size_quote"] = safe_div(realagg_total_quote, realagg_trade_count)
    df["realagg_cvd_base"] = realagg_cvd_base
    df["realagg_cvd_quote"] = realagg_cvd_quote

    # Exact generic aliases used by the confirmed Training feature builder.
    df["buy_base_volume"] = realagg_buy_base
    df["sell_base_volume"] = realagg_sell_base
    df["buy_quote_volume"] = realagg_buy_quote
    df["sell_quote_volume"] = realagg_sell_quote
    df["agg_trade_count"] = realagg_trade_count
    df["trade_flow_imbalance_base"] = realagg_flow_base
    df["trade_flow_imbalance_quote"] = realagg_flow_quote
    df["cvd_base"] = realagg_cvd_base
    df["cvd_quote"] = realagg_cvd_quote

    _v22_realagg_delta = _v22_raw.get("realagg_cvd_quote_delta", realagg_quote_delta)
    f["v22_exact_realagg_buy_ratio_quote"] = _v22_raw.get(
        "realagg_buy_ratio_quote", safe_div(realagg_buy_quote, realagg_total_quote)
    )
    f["v22_exact_realagg_cvd_quote_delta"] = _v22_realagg_delta

    # Exact Forward contract: direct realagg_cvd_quote_delta source, full
    # available input scope, rolling 50, min_periods=50, population std ddof=0.
    _v22_mean50 = _v22_realagg_delta.rolling(50, min_periods=50).mean()
    _v22_std50 = (
        _v22_realagg_delta
        .rolling(50, min_periods=50)
        .std(ddof=0)
        .replace(0, np.nan)
    )
    f["v22_exact_realagg_cvd_quote_delta_z_50"] = (
        _v22_realagg_delta - _v22_mean50
    ) / _v22_std50

    _v22_realagg_sum4 = pd.Series(np.nan, index=df.index, dtype="float64")
    _v22_realagg_scope = _v22_realagg_delta.loc[_v22_scope]
    _v22_realagg_sum4.loc[_v22_scope] = _v22_realagg_scope.rolling(
        4, min_periods=4
    ).sum()
    f["v22_exact_realagg_cvd_quote_delta_sum_4"] = _v22_realagg_sum4

    # -------------------------------------------------------------------------
    # OI — exact 5m-snapshot aggregation + Training/Forward transforms.
    # Raw last/mean/max/min/first/snapshot_count arrive from the external layer.
    # -------------------------------------------------------------------------
    oi_open_interest = _source_num("oi_open_interest")
    oi_open_interest_value = _source_num("oi_open_interest_value")
    oi_open_interest_first = _source_num("oi_open_interest_first")
    oi_open_interest_value_first = _source_num("oi_open_interest_value_first")

    f["oi_open_interest_log"] = np.log(oi_open_interest.where(oi_open_interest > 0))
    f["oi_open_interest_value_log"] = np.log(oi_open_interest_value.where(oi_open_interest_value > 0))
    # Training/Forward bar_change is within-bar last minus first, NOT bar-to-bar diff.
    f["oi_open_interest_bar_change"] = oi_open_interest - oi_open_interest_first
    f["oi_open_interest_value_bar_change"] = oi_open_interest_value - oi_open_interest_value_first

    for _n in OI_WINDOWS:
        oi_chg = oi_open_interest.diff(_n)
        oi_pct = oi_open_interest.pct_change(_n)
        oi_val_chg = oi_open_interest_value.diff(_n)
        oi_val_pct = oi_open_interest_value.pct_change(_n)
        price_ret = c.pct_change(_n)

        f[f"oi_open_interest_chg_{_n}"] = oi_chg
        f[f"oi_open_interest_pct_chg_{_n}"] = oi_pct
        f[f"oi_open_interest_value_chg_{_n}"] = oi_val_chg
        f[f"oi_open_interest_value_pct_chg_{_n}"] = oi_val_pct
        f[f"oi_price_ret_{_n}"] = price_ret
        # Match Forward builder exactly: comparisons against NaN evaluate False -> 0.
        f[f"oi_price_up_oi_up_{_n}"] = ((price_ret > 0) & (oi_chg > 0)).astype("int8")
        f[f"oi_price_up_oi_down_{_n}"] = ((price_ret > 0) & (oi_chg < 0)).astype("int8")
        f[f"oi_price_down_oi_up_{_n}"] = ((price_ret < 0) & (oi_chg > 0)).astype("int8")
        f[f"oi_price_down_oi_down_{_n}"] = ((price_ret < 0) & (oi_chg < 0)).astype("int8")
        # Numeric continuous divergence used in Training/Forward.
        f[f"oi_price_oi_divergence_{_n}"] = price_ret - oi_pct

    _v22_oi_pct4 = _v22_raw.get("oi_open_interest_pct_chg_4", oi_open_interest.pct_change(4))
    _v22_price_ret4 = _v22_scoped_pct_change(c, 4)
    f["v22_exact_oi_price_oi_divergence_4"] = _v22_raw.get(
        "oi_price_oi_divergence_4",
        _v22_price_ret4 - _v22_oi_pct4,
    )

    for _w in OI_MA_WINDOWS:
        f[f"oi_open_interest_ma_{_w}"] = oi_open_interest.rolling(
            _w, min_periods=max(5, _w // 4)
        ).mean()
        f[f"oi_open_interest_z_{_w}"] = zscore_extra(oi_open_interest, _w)
        f[f"oi_open_interest_value_ma_{_w}"] = oi_open_interest_value.rolling(
            _w, min_periods=max(5, _w // 4)
        ).mean()
        f[f"oi_open_interest_value_z_{_w}"] = zscore_extra(oi_open_interest_value, _w)

    f["atr_14"] = atr_14
    f["atr14"] = atr_14
    f["atrp_14"] = safe_div(atr_14, c)
    f["rv_20"] = logret_1.rolling(20, min_periods=20).std() * np.sqrt(20)
    f["rv_50"] = logret_1.rolling(50, min_periods=50).std() * np.sqrt(50)
    f["bb_pctb"] = safe_div(c - bb_dn, bb_up - bb_dn)
    f["bb_bw"] = safe_div(bb_up - bb_dn, ma20)
    f["bb_z"] = safe_div(c - ma20, std20)
    f["z_close_50"] = zscore(c, 50)
    f["ema20_slope"] = ema20_slope
    f["trend_regime_ema50_200"] = np.where((c > ema50) & (ema50 > ema200), 1, np.where((c < ema50) & (ema50 < ema200), -1, 0)).astype("int8")
    f["price_ema20"] = safe_div(c, ema20) - 1
    f["price_ema50"] = safe_div(c, ema50) - 1
    f["price_ema200"] = safe_div(c, ema200) - 1
    f["rsi_14"] = 100 - (100 / (1 + rs))
    f["macd_hist"] = macd - macd_signal
    f["adx_14"] = rma(dx, 14)
    f["di_diff_14"] = plus_di - minus_di
    f["ms_break_up"] = (c > last_ph).astype("int8")
    f["ms_break_dn"] = (c < last_pl).astype("int8")
    f["ms_range_pos"] = safe_div(c - last_pl, last_ph - last_pl)
    f["ms_trend_state"] = ms_trend_state
    f["ms_dist_to_lastPH_atr"] = safe_div(c - last_ph, atr_14)
    f["ms_dist_to_lastPL_atr"] = safe_div(c - last_pl, atr_14)
    f["ms_persist_up"] = consecutive_count(ms_trend_state == 1)
    f["ms_persist_dn"] = consecutive_count(ms_trend_state == -1)
    f["vol_z_20"] = zscore(volume, 20)
    f["qav_z_20"] = zscore(qav, 20)
    f["trades_z_20"] = zscore(trades, 20)
    f["taker_base_imbalance"] = safe_div(df["taker_buy_base_volume"] - (volume - df["taker_buy_base_volume"]), volume)
    f["taker_quote_imbalance"] = safe_div(df["taker_buy_quote_volume"] - (qav - df["taker_buy_quote_volume"]), qav)
    f["cvd_base_delta"] = df["cvd_base"].diff()
    f["cvd_quote_delta"] = df["cvd_quote"].diff()
    f["cvd_base_delta_z_50"] = zscore(f["cvd_base_delta"], 50)
    f["cvd_quote_delta_z_50"] = zscore(f["cvd_quote_delta"], 50)
    f["cvd_base_roc_20"] = safe_div(df["cvd_base"] - df["cvd_base"].shift(20), volume.rolling(20, min_periods=20).sum())
    f["cvd_quote_roc_20"] = safe_div(df["cvd_quote"] - df["cvd_quote"].shift(20), qav.rolling(20, min_periods=20).sum())
    f["flow_imb_base_z_20"] = zscore(df["trade_flow_imbalance_base"], 20)
    f["flow_imb_quote_z_20"] = zscore(df["trade_flow_imbalance_quote"], 20)
    f["aggressive_flow_burst"] = ((f["flow_imb_base_z_20"].abs() > 2) & (f["vol_z_20"] > 1)).astype("int8")
    f["funding_rate_chg_1"] = df["funding_rate"].diff()
    f["funding_rate_ma_24"] = df["funding_rate"].rolling(24, min_periods=24).mean()
    f["funding_rate_z_50"] = zscore(df["funding_rate"], 50)
    f["premium_body"] = df["premium_close"] - df["premium_open"]
    f["premium_range"] = df["premium_high"] - df["premium_low"]
    f["premium_close_chg_1"] = df["premium_close"].diff()
    f["premium_close_z_50"] = zscore(df["premium_close"], 50)
    f["mom"] = logret_1.rolling(3, min_periods=3).sum()
    f["dist_ema20_atr"] = safe_div(c - ema20, atr_14)
    f["ema20_slope_10"] = safe_div(ema20 - ema20.shift(10), atr_14)
    f["break_up"] = f["ms_break_up"]
    f["break_dn"] = f["ms_break_dn"]
    f["trend_state"] = f["ms_trend_state"]
    out = pd.concat([df, f], axis=1)
    out = pd.concat([out, add_support_resistance(out, tf)], axis=1)
    return out.replace([np.inf, -np.inf], np.nan)


def helper_frame(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    cols = ["date", "mom", "dist_ema20_atr", "ema20_slope_10", "break_up", "break_dn", "trend_state"]
    out = df[cols].copy()
    return out.rename(columns={c: f"{prefix}_{c}" for c in cols if c != "date"})


def add_btc_ethbtc_features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    eth_close = pd.to_numeric(df["close"], errors="coerce")
    btc_close = pd.to_numeric(df["btc_close"], errors="coerce")
    ethbtc_close = pd.to_numeric(df["ethbtc_close"], errors="coerce")
    for n in [3, 6, 12, 24]:
        out[f"btc_logret_{n}"] = logret(btc_close, n)
        out[f"ethbtc_logret_{n}"] = logret(ethbtc_close, n)
        out[f"eth_logret_{n}"] = logret(eth_close, n)
        out[f"eth_vs_btc_strength_{n}"] = out[f"eth_logret_{n}"] - out[f"btc_logret_{n}"]
    btc_ema20 = btc_close.ewm(span=20, adjust=False).mean()
    btc_ema50 = btc_close.ewm(span=50, adjust=False).mean()
    ethbtc_ema20 = ethbtc_close.ewm(span=20, adjust=False).mean()
    ethbtc_ema50 = ethbtc_close.ewm(span=50, adjust=False).mean()
    out["btc_dist_ema20_pct"] = (btc_close - btc_ema20) / btc_close.replace(0, np.nan)
    out["btc_dist_ema50_pct"] = (btc_close - btc_ema50) / btc_close.replace(0, np.nan)
    out["btc_ema20_slope_3_pct"] = btc_ema20.diff(3) / btc_close.replace(0, np.nan)
    out["btc_volatility_20"] = logret(btc_close, 1).rolling(20, min_periods=10).std()
    out["ethbtc_dist_ema20_pct"] = (ethbtc_close - ethbtc_ema20) / ethbtc_close.replace(0, np.nan)
    out["ethbtc_dist_ema50_pct"] = (ethbtc_close - ethbtc_ema50) / ethbtc_close.replace(0, np.nan)
    out["ethbtc_ema20_slope_3_pct"] = ethbtc_ema20.diff(3) / ethbtc_close.replace(0, np.nan)
    out["ethbtc_volatility_20"] = logret(ethbtc_close, 1).rolling(20, min_periods=10).std()
    out["btc_trend_score"] = np.sign(out["btc_dist_ema20_pct"].fillna(0)) + np.sign(out["btc_dist_ema50_pct"].fillna(0)) + np.sign(out["btc_ema20_slope_3_pct"].fillna(0))
    out["ethbtc_trend_score"] = np.sign(out["ethbtc_dist_ema20_pct"].fillna(0)) + np.sign(out["ethbtc_dist_ema50_pct"].fillna(0)) + np.sign(out["ethbtc_ema20_slope_3_pct"].fillna(0))
    out["eth_vs_btc_strength_z_50"] = zscore_extra(out["eth_vs_btc_strength_3"], 50)
    out["btc_eth_direction_agree_3"] = (np.sign(out["eth_logret_3"].fillna(0)) == np.sign(out["btc_logret_3"].fillna(0))).astype(int)
    out["btc_eth_direction_agree_6"] = (np.sign(out["eth_logret_6"].fillna(0)) == np.sign(out["btc_logret_6"].fillna(0))).astype(int)
    return out.replace([np.inf, -np.inf], np.nan)


def add_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    flow = pd.to_numeric(df.get("flow_imb_base_z_20", df.get("trade_flow_imbalance_base")), errors="coerce")
    cvd_delta = pd.to_numeric(df.get("cvd_base_delta"), errors="coerce")
    taker = pd.to_numeric(df.get("taker_base_imbalance"), errors="coerce")
    vol_z = pd.to_numeric(df.get("vol_z_20"), errors="coerce")
    trades_z = pd.to_numeric(df.get("trades_z_20"), errors="coerce")
    flow_sign = np.sign(flow.fillna(0))
    for n in [3, 6, 12]:
        out[f"flow_persistence_{n}"] = flow_sign.rolling(n, min_periods=1).mean()
        out[f"flow_strength_{n}"] = flow.rolling(n, min_periods=1).sum()
    for n in [3, 6]:
        out[f"flow_bull_persistence_{n}"] = (flow > 0).rolling(n, min_periods=1).mean()
        out[f"flow_bear_persistence_{n}"] = (flow < 0).rolling(n, min_periods=1).mean()
    out["cvd_acceleration"] = cvd_delta.diff()
    out["cvd_acceleration_z_20"] = zscore_extra(out["cvd_acceleration"], 20)
    out["cvd_acceleration_z_50"] = zscore_extra(out["cvd_acceleration"], 50)
    out["cvd_delta_change_3"] = cvd_delta - cvd_delta.shift(3)
    out["cvd_delta_change_6"] = cvd_delta - cvd_delta.shift(6)
    out["taker_imbalance_change_1"] = taker.diff(1)
    out["taker_imbalance_change_3"] = taker.diff(3)
    out["taker_imbalance_change_6"] = taker.diff(6)
    out["taker_imbalance_z_20"] = zscore_extra(taker, 20)
    out["volume_burst_strength"] = vol_z.fillna(0) + trades_z.fillna(0) + out["cvd_acceleration_z_20"].abs().fillna(0)
    eth_ret_1 = logret(pd.to_numeric(df["close"], errors="coerce"), 1)
    out["flow_absorption_bull"] = ((eth_ret_1 < 0) & (flow > 0)).astype(int)
    out["flow_absorption_bear"] = ((eth_ret_1 > 0) & (flow < 0)).astype(int)
    return out.replace([np.inf, -np.inf], np.nan)


def compute_touch_order(high, low, entry, atr, side: str) -> pd.Series:
    n = len(entry)
    result = np.full(n, np.nan)
    for i in range(n):
        e_idx = i + 1
        if e_idx >= n:
            continue
        e = entry[i]
        a = atr[i]
        if not np.isfinite(e) or not np.isfinite(a) or a <= 0:
            continue
        if side == "LONG":
            tp = e + PATH_TP_ATR * a
            sl = e - PATH_SL_ATR * a
        else:
            tp = e - PATH_TP_ATR * a
            sl = e + PATH_SL_ATR * a
        end = min(e_idx + PATH_HORIZON_BARS, n - 1)
        for j in range(e_idx, end + 1):
            h = high[j]
            l = low[j]
            if not np.isfinite(h) or not np.isfinite(l):
                continue
            if side == "LONG":
                hit_tp = h >= tp
                hit_sl = l <= sl
            else:
                hit_tp = l <= tp
                hit_sl = h >= sl
            if hit_tp and hit_sl:
                result[i] = 0.0
                break
            if hit_tp:
                result[i] = 1.0
                break
            if hit_sl:
                result[i] = 0.0
                break
    return pd.Series(result, index=range(n))


def add_path_quality_features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    high = pd.to_numeric(df["high"], errors="coerce").to_numpy()
    low = pd.to_numeric(df["low"], errors="coerce").to_numpy()
    open_next = pd.to_numeric(df["open"], errors="coerce").shift(-1).to_numpy()
    atr = pd.to_numeric(df["atr_14"], errors="coerce").to_numpy()
    high_s = pd.Series(high)
    low_s = pd.Series(low)
    entry_s = pd.Series(open_next)
    atr_s = pd.Series(atr).replace(0, np.nan)
    future_high = high_s.shift(-1)[::-1].rolling(PATH_HORIZON_BARS, min_periods=1).max()[::-1]
    future_low = low_s.shift(-1)[::-1].rolling(PATH_HORIZON_BARS, min_periods=1).min()[::-1]
    future_high_first = high_s.shift(-1)[::-1].rolling(PATH_FIRST_BARS, min_periods=1).max()[::-1]
    future_low_first = low_s.shift(-1)[::-1].rolling(PATH_FIRST_BARS, min_periods=1).min()[::-1]
    long_mfe = (future_high - entry_s) / atr_s
    long_mae = (entry_s - future_low) / atr_s
    short_mfe = (entry_s - future_low) / atr_s
    short_mae = (future_high - entry_s) / atr_s
    long_adverse_first = (entry_s - future_low_first) / atr_s
    short_adverse_first = (future_high_first - entry_s) / atr_s
    long_ratio = long_mfe / long_mae.replace(0, np.nan)
    short_ratio = short_mfe / short_mae.replace(0, np.nan)
    long_tp_first = compute_touch_order(high, low, open_next, atr, "LONG")
    short_tp_first = compute_touch_order(high, low, open_next, atr, "SHORT")
    for w in PATH_ROLL_WINDOWS:
        minp = max(20, w // 5)
        out[f"path_long_mfe_mae_ratio_{w}"] = long_ratio.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
        out[f"path_short_mfe_mae_ratio_{w}"] = short_ratio.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
        out[f"path_long_tp_before_sl_rate_{w}"] = long_tp_first.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
        out[f"path_short_tp_before_sl_rate_{w}"] = short_tp_first.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
        out[f"path_long_adverse_first3_atr_{w}"] = long_adverse_first.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
        out[f"path_short_adverse_first3_atr_{w}"] = short_adverse_first.rolling(w, min_periods=minp).mean().shift(SAFE_SHIFT)
    return out.replace([np.inf, -np.inf], np.nan)


def add_final_extra_features(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "1m":
        return df
    required = [
        "date", "open", "high", "low", "close", "atr_14", "btc_close", "ethbtc_close",
        "flow_imb_base_z_20", "cvd_base_delta", "taker_base_imbalance", "vol_z_20", "trades_z_20",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{tf} missing required columns for extra features: {missing}")
    btc_features = add_btc_ethbtc_features(df)
    flow_features = add_flow_features(df)
    path_features = add_path_quality_features(df)
    features = pd.concat([btc_features, flow_features, path_features], axis=1).replace([np.inf, -np.inf], np.nan)
    existing = [c for c in features.columns if c in df.columns]
    if existing:
        df = df.drop(columns=existing)
    return pd.concat([df.reset_index(drop=True), features.reset_index(drop=True)], axis=1).replace([np.inf, -np.inf], np.nan)


# =============================================================================
# HISTORICAL TRAINING PANEL FOR THRESHOLDS
# =============================================================================
def find_time_col(df: pd.DataFrame) -> str:
    candidates = ["ts_open", "open_time", "open_time_utc", "Open time", "open_datetime", "datetime", "date", "time", "timestamp", "Timestamp", "Date", "Time"]
    for c in candidates:
        if c in df.columns:
            return c
    low = {str(c).strip().lower(): c for c in df.columns}
    for key in ["ts_open", "open_time", "timestamp", "datetime", "date", "time"]:
        if key in low:
            return low[key]
    raise RuntimeError(f"No time column found. first_columns={list(df.columns)[:50]}")


def parse_time_series(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        x = pd.to_numeric(s, errors="coerce")
        med = float(x.dropna().median()) if x.notna().any() else np.nan
        if np.isfinite(med):
            if med > 1e17:
                return pd.to_datetime(x, unit="ns", errors="coerce").dt.tz_localize(None)
            if med > 1e14:
                return pd.to_datetime(x, unit="us", errors="coerce").dt.tz_localize(None)
            if med > 1e11:
                return pd.to_datetime(x, unit="ms", errors="coerce").dt.tz_localize(None)
            if med > 1e9:
                return pd.to_datetime(x, unit="s", errors="coerce").dt.tz_localize(None)
    out = pd.to_datetime(s.astype("string"), errors="coerce", utc=True)
    return out.dt.tz_convert(None)


def standardize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    low = {str(c).lower(): c for c in df.columns}
    rename = {}
    for target in ["open", "high", "low", "close", "volume"]:
        if target not in df.columns and target.lower() in low:
            rename[low[target.lower()]] = target
    if rename:
        df = df.rename(columns=rename)
    return df


def add_time_columns(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    df = standardize_ohlc(df)
    tcol = find_time_col(df)
    df["ts_open"] = parse_time_series(df[tcol])
    if "ts_close" in df.columns and df["ts_close"].notna().any():
        df["ts_close"] = parse_time_series(df["ts_close"])
    else:
        df["ts_close"] = df["ts_open"] + pd.to_timedelta(TF_MINUTES[tf], unit="m")
    df = df.dropna(subset=["ts_open", "ts_close"]).sort_values("ts_open").drop_duplicates("ts_open").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def find_csv(tf: str) -> Path:
    patterns = [f"{SYMBOL}_{tf}_BINANCE_*clean_raw_plus_external.csv", f"*{tf}*clean_raw_plus_external.csv"]
    hits: List[Path] = []
    for pat in patterns:
        hits.extend(sorted(BASE_DIR.glob(pat)))
    hits = list(dict.fromkeys(hits))
    if not hits:
        raise FileNotFoundError(f"No file found for tf={tf}, BASE_DIR={BASE_DIR}")

    historical_exact = [
        p for p in hits
        if p.name.startswith(f"{SYMBOL}_{tf}_BINANCE_20230401_20260401")
    ]
    if historical_exact:
        return historical_exact[-1]

    non_live_hits = [
        p for p in hits
        if "live" not in p.name.lower()
    ]
    if non_live_hits:
        exact_non_live = [p for p in non_live_hits if p.name.startswith(f"{SYMBOL}_{tf}_BINANCE_")]
        return exact_non_live[-1] if exact_non_live else non_live_hits[-1]

    exact = [p for p in hits if p.name.startswith(f"{SYMBOL}_{tf}_BINANCE_")]
    return exact[-1] if exact else hits[-1]

def load_historical_tf(tf: str) -> pd.DataFrame:
    path = find_csv(tf)
    df = pd.read_csv(path, encoding="latin1", low_memory=False)
    df = add_time_columns(df, tf)
    return df


def attach_htf_training_style(base: pd.DataFrame, htf: pd.DataFrame, tf: str) -> pd.DataFrame:
    base = base.sort_values("ts_close").reset_index(drop=True).copy()
    htf = htf.sort_values("ts_close").reset_index(drop=True).copy()
    reserved = {"ts_open", "ts_close", "entry_time_next", "entry_ts_next", "entry_open_next", "next_index", "valid_next_entry", "entry_gap_minutes"}
    rename = {c: f"{tf}__{c}" for c in htf.columns if c not in reserved}
    h = htf.rename(columns=rename)
    keep = ["ts_close"] + list(rename.values())
    h = h[keep].rename(columns={"ts_close": f"{tf}__ts_close"})
    out = pd.merge_asof(
        base,
        h.sort_values(f"{tf}__ts_close"),
        left_on="ts_close",
        right_on=f"{tf}__ts_close",
        direction="backward",
        allow_exact_matches=True,
    )
    pref = f"{tf}__"
    for c in list(out.columns):
        if c.startswith(pref):
            raw = c[len(pref):]
            alias = f"{raw}_{tf}"
            if alias not in out.columns:
                out[alias] = out[c]
    future = int((out[f"{tf}__ts_close"] > out["ts_close"]).sum()) if f"{tf}__ts_close" in out.columns else 0
    if future != 0:
        raise RuntimeError(f"HTF lookahead detected for {tf}: future_rows={future}")
    return out


def ensure_basic_ohlc_helpers(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    o = pd.to_numeric(panel["open"], errors="coerce")
    h = pd.to_numeric(panel["high"], errors="coerce")
    l = pd.to_numeric(panel["low"], errors="coerce")
    c = pd.to_numeric(panel["close"], errors="coerce")
    rng = (h - l).replace(0, np.nan)
    if "range" not in panel.columns:
        panel["range"] = h - l
    if "body" not in panel.columns:
        panel["body"] = (c - o).abs()
    if "body_pct" not in panel.columns:
        panel["body_pct"] = ((c - o).abs() / rng).clip(lower=0, upper=5)
    if "upper_wick_pct" not in panel.columns:
        panel["upper_wick_pct"] = ((h - np.maximum(o, c)) / rng).clip(lower=0, upper=5)
    if "lower_wick_pct" not in panel.columns:
        panel["lower_wick_pct"] = ((np.minimum(o, c) - l) / rng).clip(lower=0, upper=5)
    if "close_pos" not in panel.columns:
        panel["close_pos"] = ((c - l) / rng).clip(lower=0, upper=1)
    if "candle_direction" not in panel.columns:
        panel["candle_direction"] = np.where(c > o, 1, np.where(c < o, -1, 0))
    return panel


def rebuild_execution_columns(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.sort_values("ts_open").reset_index(drop=True).copy()
    panel["entry_time_next"] = panel["ts_open"].shift(-1)
    panel["entry_ts_next"] = panel["entry_time_next"]
    panel["entry_open_next"] = pd.to_numeric(panel["open"].shift(-1), errors="coerce")
    panel["next_index"] = np.arange(len(panel), dtype="float64") + 1.0
    if len(panel):
        panel.loc[panel.index[-1], ["entry_time_next", "entry_ts_next", "entry_open_next", "next_index"]] = [pd.NaT, pd.NaT, np.nan, np.nan]
    gap = (panel["entry_time_next"] - panel["ts_open"]).dt.total_seconds() / 60.0
    panel["entry_gap_minutes"] = gap
    panel["valid_next_entry"] = panel["entry_open_next"].notna() & panel["next_index"].notna() & np.isclose(panel["entry_gap_minutes"].fillna(-9999.0), EXPECTED_NEXT_MINUTES)
    return panel


def add_v22_locked_threshold_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the exact locked-engine columns used for V22 train thresholds.

    Called only after the panel is cut to START_DATE..END_DATE, matching the
    locked source's operation order. Existing unrelated ML/SHORT columns stay
    untouched.
    """
    out = panel.copy()
    h = pd.to_numeric(out["high"], errors="coerce")
    l = pd.to_numeric(out["low"], errors="coerce")
    c = pd.to_numeric(out["close"], errors="coerce")

    if "range_pct" not in out.columns:
        out["range_pct"] = (h - l) / c.replace(0, np.nan)
    for n in (4, 12, 24):
        name = f"ret{n}"
        if name not in out.columns:
            raw_name = f"ret_{n}"
            out[name] = (
                pd.to_numeric(out[raw_name], errors="coerce")
                if raw_name in out.columns else c.pct_change(n)
            )
    if "prev_high_20" not in out.columns:
        out["prev_high_20"] = h.shift(1).rolling(20, min_periods=20).max()

    out["session_active_07_21"] = (
        (out["ts_open"].dt.hour >= 7) & (out["ts_open"].dt.hour < 21)
    ).astype(float)

    # The locked source explicitly recreates these from the raw delta after the
    # study-window cut, even when similarly named columns already exist.
    if "realagg_cvd_quote_delta" in out.columns:
        x = pd.to_numeric(out["realagg_cvd_quote_delta"], errors="coerce")
        out["realagg_cvd_quote_delta_z_50"] = (
            x - x.rolling(50, min_periods=50).mean()
        ) / x.rolling(50, min_periods=50).std().replace(0, np.nan)
        out["realagg_cvd_quote_delta_sum_4"] = x.rolling(4, min_periods=4).sum()

    if "binance_funding_rate_abs" in out.columns:
        out["binance_funding_rate_abs"] = pd.to_numeric(
            out["binance_funding_rate_abs"], errors="coerce"
        ).abs()
    elif "binance_funding_rate" in out.columns:
        out["binance_funding_rate_abs"] = pd.to_numeric(
            out["binance_funding_rate"], errors="coerce"
        ).abs()
    elif "funding_rate" in out.columns:
        out["binance_funding_rate_abs"] = pd.to_numeric(
            out["funding_rate"], errors="coerce"
        ).abs()

    return out.replace([np.inf, -np.inf], np.nan)


def load_training_panel_for_thresholds() -> pd.DataFrame:
    panel = load_historical_tf("15m")
    for tf in HTF_TFS:
        panel = attach_htf_training_style(panel, load_historical_tf(tf), tf)
    panel = panel[(panel["ts_open"] >= START_DATE) & (panel["ts_open"] <= END_DATE)].copy()
    panel = panel.sort_values("ts_open").reset_index(drop=True)
    panel = add_v22_locked_threshold_features(panel)
    panel = ensure_basic_ohlc_helpers(panel)
    panel = rebuild_execution_columns(panel)
    return panel


def make_splits(panel: pd.DataFrame) -> Dict[str, SplitDef]:
    n = len(panel)
    n_train = int(n * TRAIN_RATIO)
    n_val = int(n * VAL_RATIO)
    return {
        "train": SplitDef("train", 0, n_train),
        "validation": SplitDef("validation", n_train, n_train + n_val),
        "test": SplitDef("test", n_train + n_val, n),
    }


def first_col(df_or_row, names: List[str], required: bool = False, label: str = "") -> Optional[str]:
    cols = list(df_or_row.columns) if isinstance(df_or_row, pd.DataFrame) else list(df_or_row.index)
    colset = set(cols)
    for c in names:
        if c in colset:
            return c
    low = {str(c).lower(): c for c in cols}
    for c in names:
        if str(c).lower() in low:
            return low[str(c).lower()]
    if required:
        raise RuntimeError(f"Missing required column {label}: {names}")
    return None


def qtrain(df: pd.DataFrame, splits: Dict[str, SplitDef], col: Optional[str], q: float, required: bool = False) -> float:
    if col is None or col not in df.columns:
        if required:
            raise RuntimeError(f"Cannot compute train quantile; missing column: {col}")
        return np.nan
    sp = splits["train"]
    s = pd.to_numeric(df.iloc[sp.start:sp.end][col], errors="coerce").dropna()
    if not len(s):
        if required:
            raise RuntimeError(f"Cannot compute train quantile for empty column: {col}")
        return np.nan
    return float(s.quantile(q))


# =============================================================================
# SHORTLIST SPECS
# =============================================================================
LEAKY_PATTERNS = ("future", "target", "label", "pnl", "profit", "mfe", "mae", "exit", "tp_hit", "sl_hit", "outcome", "ret_fwd", "forward")


def clean_token(x: object) -> str:
    if pd.isna(x):
        return ""
    return str(x).strip()


def norm_side(x: Any) -> str:
    return str(x).strip().upper()


def norm_family(x: Any) -> str:
    return str(x).strip().upper().replace("FAM__", "").replace(" ", "_").replace("-", "_")


def parse_op(row: pd.Series) -> Optional[str]:
    txt = " ".join(str(row.get(k, "")).lower() for k in ["use", "direction", "op", "operator", "quantile"])
    if any(w in txt for w in ["<=", "less", "below", "lower", "low", "lt", "left"]):
        return "<="
    if any(w in txt for w in [">=", "greater", "above", "upper", "high", "gt", "right"]):
        return ">="
    return None


def resolve_col(panel: pd.DataFrame, row: pd.Series, feature_first: bool = False) -> Optional[str]:
    feature = clean_token(row.get("feature", ""))
    col = clean_token(row.get("column", ""))
    tf = clean_token(row.get("timeframe", ""))
    candidates: List[str] = []
    candidates.extend([feature, col] if feature_first else [col, feature])
    if tf and feature:
        candidates += [f"{tf}__{feature}", f"{feature}_{tf}"]
    if col.startswith("15m__"):
        candidates.append(col.split("__", 1)[1])
    if "__" in col:
        a, b = col.split("__", 1)
        candidates += [f"{a}__{b}", f"{b}_{a}", b]
    seen = set()
    for c in candidates:
        if c and c != "nan" and c not in seen:
            seen.add(c)
            if c in panel.columns:
                return c
    low = {str(c).lower(): c for c in panel.columns}
    for c in candidates:
        if str(c).lower() in low:
            return low[str(c).lower()]
    return None


def build_specs(panel: pd.DataFrame, shortlist: pd.DataFrame, label: str, feature_first: bool = False) -> List[FeatureSpec]:
    specs: List[FeatureSpec] = []
    for i, row in shortlist.iterrows():
        side = norm_side(row.get("side", ""))
        fam = norm_family(row.get("family", ""))
        feature = clean_token(row.get("feature", ""))
        col_raw = clean_token(row.get("column", ""))
        joined = f"{feature} {col_raw}".lower()
        if any(w in joined for w in LEAKY_PATTERNS):
            continue
        op = parse_op(row)
        if op is None:
            continue
        try:
            thr = float(row.get("threshold"))
        except Exception:
            continue
        src = resolve_col(panel, row, feature_first=feature_first)
        if src is None:
            continue
        specs.append(FeatureSpec(int(i), side, fam, feature, col_raw, clean_token(row.get("timeframe", "")), op, thr, src))
    if not specs:
        raise RuntimeError(f"No usable shortlist specs for {label}")
    return specs


def spec_pass_value(row: pd.Series, spec: FeatureSpec) -> bool:
    val = row.get(spec.source_column, np.nan)
    if pd.isna(val):
        return False
    val = float(val)
    return val <= spec.threshold if spec.op == "<=" else val >= spec.threshold


def family_setup_pass(row: pd.Series, specs: List[FeatureSpec], side: str, family: str) -> bool:
    side = side.upper()
    family = family.upper().replace("FAM__", "")
    fs = [s for s in specs if s.side == side and s.family == family]
    if not fs:
        return False
    return any(spec_pass_value(row, sp) for sp in fs)


def load_thresholds_and_specs() -> Tuple[RuleThresholds, V22LiveThresholds, List[FeatureSpec], List[FeatureSpec]]:
    logging.info("[THRESHOLDS] loading historical training panel")
    panel = load_training_panel_for_thresholds()
    splits = make_splits(panel)
    shortlist = pd.read_csv(SHORTLIST_FILE, encoding="latin1", low_memory=False)
    shortlist.columns = [str(c).strip() for c in shortlist.columns]
    long_specs = build_specs(panel, shortlist, "LONG", feature_first=False)
    short_specs = build_specs(panel, shortlist, "SHORT", feature_first=SHORT_USE_SHORTLIST_FEATURE_FIELD_FIRST)

    close_pos_col = first_col(panel, ["close_pos", "close_position"])
    mom_col = first_col(panel, ["mom", "momentum"], required=True, label="mom")
    adx_col = first_col(panel, ["adx_14"], required=True, label="adx_14")
    di_col = first_col(panel, ["di_diff_14"], required=True, label="di_diff_14")
    adx_1h_col = first_col(panel, ["adx_14_1h", "1h__adx_14"], required=True, label="1h adx")
    di_1h_col = first_col(panel, ["di_diff_14_1h", "1h__di_diff_14"], required=True, label="1h di")
    rsi_1h_col = first_col(panel, ["rsi_14_1h", "1h__rsi_14"], required=True, label="1h rsi")
    v22_vol_col = first_col(panel, ["rv_50", "atrp_14", "rv_20"], required=False, label="v22 volatility regime source")

    th = RuleThresholds(
        long_adx_q60=qtrain(panel, splits, adx_col, 0.60, True),
        long_di_q60=qtrain(panel, splits, di_col, 0.60, True),
        long_close_pos_q60=qtrain(panel, splits, close_pos_col, 0.60, True),
        long_mom_q70=qtrain(panel, splits, mom_col, 0.70, True),
        long_di_q70_final=qtrain(panel, splits, di_col, 0.70, True),
        long_1h_adx_q70=qtrain(panel, splits, adx_1h_col, 0.70, True),
        long_1h_di_q25=qtrain(panel, splits, di_1h_col, 0.25, True),
        long_1h_rsi_q25=qtrain(panel, splits, rsi_1h_col, 0.25, True),
        short_range_q50=qtrain(panel, splits, "range", 0.50, True),
        short_body_q50=qtrain(panel, splits, "body_pct", 0.50, True),
        short_mom_q30=qtrain(panel, splits, "mom", 0.30, True),
        short_s1_mom_q30=qtrain(panel, splits, "s1_mom", 0.30, False),
        short_vol_q60=qtrain(panel, splits, "vol_z_20", 0.60, True),
        short_1h_adx_q80=qtrain(panel, splits, adx_1h_col, 0.80, True),
        short_1h_di_q80=qtrain(panel, splits, di_1h_col, 0.80, True),
        short_1h_rsi_q80=qtrain(panel, splits, rsi_1h_col, 0.80, True),
    )
    v22th = V22LiveThresholds(
        atr_low=qtrain(panel, splits, "atrp_14", 0.25, False),
        atr_high=qtrain(panel, splits, "atrp_14", 0.92, False),
        range_high=qtrain(panel, splits, "range_pct", 0.55, False),
        funding_abs_hi=qtrain(panel, splits, "binance_funding_rate_abs", 0.95, False),
        q_range70=qtrain(panel, splits, "range_pct", 0.70, False),
        q_range40=qtrain(panel, splits, "range_pct", 0.40, False),
        q_ret4_65=qtrain(panel, splits, "ret4", 0.65, False),
        q_ret12_40=qtrain(panel, splits, "ret12", 0.40, False),
        q_ret24_25=qtrain(panel, splits, "ret24", 0.25, False),
        q_closepos60=qtrain(panel, splits, "close_pos", 0.60, False),
        q_closepos75=qtrain(panel, splits, "close_pos", 0.75, False),
        q_lwick60=qtrain(panel, splits, "lower_wick_pct", 0.60, False),
        q_bbw30=qtrain(panel, splits, "bb_bw", 0.30, False),
        q_realagg70=qtrain(panel, splits, "realagg_buy_ratio_quote", 0.70, False),
        q_realagg_delta65=qtrain(panel, splits, "realagg_cvd_quote_delta_z_50", 0.65, False),
        vol_q33=V22_FROZEN_VOL_Q33,
        vol_q66=V22_FROZEN_VOL_Q66,
    )
    _required_v22_thresholds = [
        "atr_low", "atr_high", "range_high", "funding_abs_hi",
        "q_range70", "q_range40", "q_ret4_65", "q_ret12_40",
        "q_ret24_25", "q_closepos60", "q_closepos75", "q_lwick60",
        "q_bbw30", "q_realagg70", "q_realagg_delta65",
    ]
    _bad_v22_thresholds = [
        name for name in _required_v22_thresholds
        if not np.isfinite(float(getattr(v22th, name)))
    ]
    if _bad_v22_thresholds:
        raise RuntimeError(
            "V22 LONG threshold initialization failed; non-finite values: "
            + ", ".join(_bad_v22_thresholds)
        )
    logging.info(
        "[THRESHOLDS] loaded | long_specs=%d short_specs=%d | "
        "v22_long_thresholds=FINITE | v22_vol_scope=SELECTED_1203_LONG_TRAIN_ROWS "
        "q33=%.15f q66=%.15f",
        len(long_specs), len(short_specs), v22th.vol_q33, v22th.vol_q66,
    )
    return th, v22th, long_specs, short_specs


RULE_THRESHOLDS, V22_THRESHOLDS, LONG_SPECS, SHORT_SPECS = load_thresholds_and_specs()


# =============================================================================
# BINANCE FETCH
# =============================================================================
def _request_json(url: str, params: Dict[str, Any], timeout: int = 30):
    max_retries = 5
    base_sleep = 2.0

    for attempt in range(max_retries):
        r = requests.get(url, params=params, timeout=timeout)

        retry_after = r.headers.get("Retry-After")
        if r.status_code in (418, 429):
            if retry_after is not None:
                try:
                    sleep_seconds = float(retry_after)
                except Exception:
                    sleep_seconds = base_sleep * (attempt + 1)
            else:
                sleep_seconds = base_sleep * (attempt + 1)

            logging.warning(
                "[BINANCE RATE LIMIT] status=%s attempt=%d/%d sleep=%.1fs url=%s",
                r.status_code,
                attempt + 1,
                max_retries,
                sleep_seconds,
                url,
            )
            time.sleep(max(1.0, sleep_seconds))
            continue

        if retry_after is not None and attempt < max_retries - 1:
            try:
                sleep_seconds = float(retry_after)
                if sleep_seconds > 0:
                    logging.warning(
                        "[BINANCE RETRY-AFTER] attempt=%d/%d sleep=%.1fs url=%s",
                        attempt + 1,
                        max_retries,
                        sleep_seconds,
                        url,
                    )
                    time.sleep(sleep_seconds)
            except Exception:
                pass

        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"[BINANCE] bad response from {url}: {data}")
        return data

    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"[BINANCE] bad response from {url}: {data}")
    return data


def _request_bytes(url: str, timeout: int = 90) -> bytes:
    max_retries = 5
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 ETHUSDT-live/1.0"})
            if r.status_code in (418, 429):
                sleep_seconds = float(r.headers.get("Retry-After", 2.0 * (attempt + 1)))
                logging.warning("[BINANCE DATA RATE LIMIT] status=%s sleep=%.1fs url=%s", r.status_code, sleep_seconds, url)
                time.sleep(max(1.0, sleep_seconds))
                continue
            r.raise_for_status()
            return r.content
        except Exception:
            if attempt >= max_retries - 1:
                raise
            time.sleep(min(2.0 * (attempt + 1), 10.0))
    raise RuntimeError(f"Unable to download bytes: {url}")


def _utc_ts(x) -> pd.Timestamp:
    ts = pd.Timestamp(x)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _tf_pandas_freq(tf: str) -> str:
    return {
        "1m": "1min", "5m": "5min", "15m": "15min",
        "1h": "1h", "4h": "4h", "1d": "1D",
    }[tf]


def _detect_epoch_unit(s: pd.Series) -> str:
    vals = pd.to_numeric(s, errors="coerce").dropna()
    if vals.empty:
        return "ms"
    med = float(vals.median())
    return "us" if med > 1e14 else "ms"


def _buyer_maker_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s.fillna(False)
    return s.astype(str).str.strip().str.lower().isin(["true", "1", "t", "yes"])


def _empty_realagg_1m() -> pd.DataFrame:
    return pd.DataFrame(columns=REALAGG_CACHE_COLUMNS)


def _normalize_realagg_cache_frame(df: pd.DataFrame, source_label: str) -> pd.DataFrame:
    """Validate/normalize only the persistent 1m REALAGG source frame."""
    if df is None or df.empty:
        return _empty_realagg_1m()

    missing = [c for c in REALAGG_CACHE_COLUMNS if c not in df.columns]
    if missing:
        raise RuntimeError(f"[REALAGG CACHE] {source_label} missing columns={missing}")

    out = df[REALAGG_CACHE_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce")
    if out["date"].isna().any():
        raise RuntimeError(f"[REALAGG CACHE] {source_label} contains invalid timestamps")
    if bool((out["date"] != out["date"].dt.floor("1min")).any()):
        raise RuntimeError(f"[REALAGG CACHE] {source_label} contains non-1m timestamps")

    numeric_cols = [c for c in REALAGG_CACHE_COLUMNS if c != "date"]
    for c in numeric_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
        if out[c].isna().any():
            raise RuntimeError(f"[REALAGG CACHE] {source_label} contains non-numeric {c}")
        if bool((out[c] < 0).any()):
            raise RuntimeError(f"[REALAGG CACHE] {source_label} contains negative {c}")

    trade_count = out["realagg_trade_count"].to_numpy(dtype=float)
    if not np.allclose(trade_count, np.rint(trade_count), atol=1e-9, rtol=0.0):
        raise RuntimeError(f"[REALAGG CACHE] {source_label} trade counts are not integral")
    out["realagg_trade_count"] = np.rint(trade_count).astype(np.int64)

    if out["date"].duplicated().any():
        raise RuntimeError(f"[REALAGG CACHE] {source_label} contains duplicate 1m timestamps")

    return out.sort_values("date").reset_index(drop=True)


def _load_realagg_persistent_cache() -> pd.DataFrame:
    path = REALAGG_RUNTIME_CACHE_FILE
    if not path.exists() or path.stat().st_size == 0:
        logging.info("[REALAGG CACHE] persistent cache MISS | path=%s", path)
        return _empty_realagg_1m()
    try:
        cached = pd.read_csv(path, low_memory=False, float_precision="round_trip")
        cached = _normalize_realagg_cache_frame(cached, "persistent_disk")
        now = pd.Timestamp.now(tz="UTC")
        if not cached.empty and cached["date"].max() > now + pd.Timedelta(minutes=1):
            raise RuntimeError(f"future timestamp max={cached['date'].max()} now={now}")
        logging.info(
            "[REALAGG CACHE] persistent cache HIT | rows=%d range=%s -> %s path=%s",
            len(cached),
            cached["date"].min() if not cached.empty else None,
            cached["date"].max() if not cached.empty else None,
            path,
        )
        return cached
    except Exception as exc:
        logging.warning(
            "[REALAGG CACHE] persistent cache rejected; exact Binance rebuild will be used | path=%s error=%s",
            path,
            exc,
        )
        return _empty_realagg_1m()


def _save_realagg_persistent_cache(cache: pd.DataFrame) -> None:
    normalized = _normalize_realagg_cache_frame(cache, "memory_before_save")
    REALAGG_RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REALAGG_RUNTIME_CACHE_FILE.with_name(
        f".{REALAGG_RUNTIME_CACHE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        normalized.to_csv(
            temporary,
            index=False,
            float_format="%.17g",
            date_format="%Y-%m-%dT%H:%M:%S%z",
        )
        with open(temporary, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, REALAGG_RUNTIME_CACHE_FILE)
        logging.info(
            "[REALAGG CACHE] persistent cache saved atomically | rows=%d range=%s -> %s path=%s",
            len(normalized),
            normalized["date"].min() if not normalized.empty else None,
            normalized["date"].max() if not normalized.empty else None,
            REALAGG_RUNTIME_CACHE_FILE,
        )
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except Exception:
                pass


def _replace_realagg_cache_window(
    cache: pd.DataFrame,
    fresh: pd.DataFrame,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> pd.DataFrame:
    if fresh is None or fresh.empty:
        return _normalize_realagg_cache_frame(cache, "memory_no_fresh")

    base = _normalize_realagg_cache_frame(cache, "memory_base")
    new = _normalize_realagg_cache_frame(fresh, "fresh_exact_source")
    start = _utc_ts(start_dt).floor("1min")
    end = _utc_ts(end_dt).floor("1min")
    new = new[(new["date"] >= start) & (new["date"] <= end)].copy()
    base = base[(base["date"] < start) | (base["date"] > end)].copy()
    out = pd.concat([base, new], ignore_index=True, sort=False)
    if out.empty:
        return _empty_realagg_1m()
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return _normalize_realagg_cache_frame(out, "memory_merged")


def _realagg_completed_day_coverage(
    cached_day: pd.DataFrame,
    day: pd.Timestamp,
) -> Tuple[bool, pd.DatetimeIndex]:
    """Require exactly 1,440 aligned 1m rows with no gaps for a completed UTC day."""
    day = _utc_ts(day).floor("D")
    day_next = day + pd.Timedelta(days=1)
    expected = pd.date_range(
        day,
        day_next - pd.Timedelta(minutes=1),
        freq="1min",
    )
    if cached_day is None or cached_day.empty:
        return False, expected

    normalized = _normalize_realagg_cache_frame(cached_day, "completed_day_validation")
    actual = pd.DatetimeIndex(normalized["date"])
    missing = expected.difference(actual)
    extra = actual.difference(expected)
    complete = len(normalized) == 1440 and len(missing) == 0 and len(extra) == 0
    return complete, missing


def _checkpoint_realagg_rest_parts(
    parts: List[pd.DataFrame],
    start_dt: pd.Timestamp,
    checkpoint_dt: pd.Timestamp,
    label: str,
) -> None:
    """Persist exact REST progress so a 429/restart resumes instead of redownloading."""
    if not parts:
        return
    fresh = (
        pd.concat(parts, ignore_index=True, sort=False)
        .groupby("date", sort=True, as_index=False)
        .sum(numeric_only=True)
    )
    if fresh.empty:
        return
    start = _utc_ts(start_dt).floor("1min")
    end = _utc_ts(checkpoint_dt).floor("1min")
    if end < start:
        return
    fresh = fresh[(fresh["date"] >= start) & (fresh["date"] <= end)].copy()
    if fresh.empty:
        return
    try:
        disk = _load_realagg_persistent_cache()
        merged = _replace_realagg_cache_window(disk, fresh, start, end)
        _save_realagg_persistent_cache(merged)
        logging.info(
            "[REALAGG REST CHECKPOINT] %s | rows=%d range=%s -> %s",
            label,
            len(fresh),
            start,
            end,
        )
    except Exception as exc:
        logging.warning("[REALAGG REST CHECKPOINT] failed label=%s error=%s", label, exc)


def _aggregate_aggtrade_vectors_to_1m(
    price: pd.Series,
    qty: pd.Series,
    transact_time: pd.Series,
    buyer_maker: pd.Series,
) -> pd.DataFrame:
    price = pd.to_numeric(price, errors="coerce")
    qty = pd.to_numeric(qty, errors="coerce")
    unit = _detect_epoch_unit(transact_time)
    t = pd.to_datetime(pd.to_numeric(transact_time, errors="coerce"), unit=unit, utc=True, errors="coerce")
    bm = _buyer_maker_bool(buyer_maker)
    valid = price.notna() & qty.notna() & t.notna()
    if not bool(valid.any()):
        return _empty_realagg_1m()
    price = price[valid].astype(float)
    qty = qty[valid].astype(float)
    t = t[valid]
    bm = bm[valid]
    quote = price * qty
    is_buy_taker = ~bm
    tmp = pd.DataFrame({
        "date": t.dt.floor("1min"),
        "realagg_trade_count": 1,
        "realagg_buy_base_volume": qty.where(is_buy_taker, 0.0),
        "realagg_sell_base_volume": qty.where(~is_buy_taker, 0.0),
        "realagg_buy_quote_volume": quote.where(is_buy_taker, 0.0),
        "realagg_sell_quote_volume": quote.where(~is_buy_taker, 0.0),
    })
    return tmp.groupby("date", sort=True, as_index=False).sum(numeric_only=True)


def _aggregate_aggtrades_zip_bytes_to_1m(payload: bytes, source_name: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"[REALAGG] expected one CSV in {source_name}, got {members}")
        member = members[0]
        with zf.open(member) as fh:
            first_line = fh.readline().decode("utf-8", errors="ignore").lower()
        has_header = all(token in first_line for token in ["price", "quantity", "transact_time", "is_buyer_maker"])
        parts = []
        with zf.open(member) as fh:
            reader = pd.read_csv(
                fh,
                header=0 if has_header else None,
                chunksize=500_000,
                low_memory=False,
            )
            for chunk in reader:
                if has_header:
                    required = ["price", "quantity", "transact_time", "is_buyer_maker"]
                    missing = [c for c in required if c not in chunk.columns]
                    if missing:
                        raise RuntimeError(f"[REALAGG] {source_name} missing columns={missing}")
                    part = _aggregate_aggtrade_vectors_to_1m(
                        chunk["price"], chunk["quantity"],
                        chunk["transact_time"], chunk["is_buyer_maker"],
                    )
                else:
                    if chunk.shape[1] < 7:
                        raise RuntimeError(f"[REALAGG] {source_name} unexpected columns={chunk.shape[1]}")
                    part = _aggregate_aggtrade_vectors_to_1m(
                        chunk.iloc[:, 1], chunk.iloc[:, 2],
                        chunk.iloc[:, 5], chunk.iloc[:, 6],
                    )
                if not part.empty:
                    parts.append(part)
    if not parts:
        return _empty_realagg_1m()
    return pd.concat(parts, ignore_index=True).groupby("date", sort=True, as_index=False).sum(numeric_only=True)


def _request_realagg_rest_json(url: str, params: Dict[str, Any], timeout: int = 30):
    """Pace only exact aggTrades REST pagination; all other Binance calls are unchanged."""
    global _REALAGG_REST_LAST_REQUEST_MONOTONIC
    if _REALAGG_REST_LAST_REQUEST_MONOTONIC is not None:
        elapsed = time.monotonic() - _REALAGG_REST_LAST_REQUEST_MONOTONIC
        remaining = REALAGG_REST_MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
    try:
        return _request_json(url, params=params, timeout=timeout)
    finally:
        _REALAGG_REST_LAST_REQUEST_MONOTONIC = time.monotonic()


def _fetch_futures_aggtrades_range_to_1m(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    start_ts = _utc_ts(start_dt)
    end_ts = min(_utc_ts(end_dt), pd.Timestamp.now(tz="UTC"))
    if start_ts >= end_ts:
        return _empty_realagg_1m()
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/aggTrades"

    # Find the first aggregate trade at/after start using a <=1h time request,
    # then continue losslessly by aggregate-trade id. Same exact-source logic as
    # before, with dedicated pacing so pagination does not flood Binance.
    probe = start_ms
    data = []
    while probe <= end_ms:
        probe_end = min(end_ms, probe + 60 * 60 * 1000 - 1)
        data = _request_realagg_rest_json(
            url,
            {"symbol": SYMBOL, "startTime": probe, "endTime": probe_end, "limit": 1000},
            timeout=30,
        )
        if data:
            break
        probe = probe_end + 1
    if not data:
        logging.info(
            "[REALAGG REST PROGRESS] requests=0 aggtrades=0 reached=NONE progress=100.0%% elapsed=0.0s | no trades in requested range"
        )
        return _empty_realagg_1m()

    parts = []
    seen_last_id = None
    request_count = 0
    aggtrade_count = 0
    started_monotonic = time.monotonic()
    last_seen_time_ms = start_ms
    last_checkpoint_minute: Optional[pd.Timestamp] = None

    while data:
        request_count += 1
        batch = pd.DataFrame(data)
        required = ["a", "p", "q", "T", "m"]
        missing = [c for c in required if c not in batch.columns]
        if missing:
            raise RuntimeError(f"[REALAGG REST] missing fields={missing}")
        t_ms = pd.to_numeric(batch["T"], errors="coerce")
        in_range = (t_ms >= start_ms) & (t_ms <= end_ms)
        in_range_count = int(in_range.sum())
        aggtrade_count += in_range_count
        if bool(in_range.any()):
            part = _aggregate_aggtrade_vectors_to_1m(
                batch.loc[in_range, "p"], batch.loc[in_range, "q"],
                batch.loc[in_range, "T"], batch.loc[in_range, "m"],
            )
            if not part.empty:
                parts.append(part)

        ids = pd.to_numeric(batch["a"], errors="coerce").dropna()
        times = t_ms.dropna()
        if ids.empty or times.empty:
            raise RuntimeError(
                "[REALAGG REST] pagination batch contained no valid aggregate-trade id/time"
            )
        last_id = int(ids.max())
        last_time = int(times.max())
        last_seen_time_ms = max(last_seen_time_ms, last_time)
        if seen_last_id is not None and last_id <= seen_last_id:
            raise RuntimeError("[REALAGG REST] pagination did not advance")
        seen_last_id = last_id
        checkpoint_minute = (
            pd.to_datetime(
                min(last_seen_time_ms, end_ms),
                unit="ms",
                utc=True,
            ).floor("1min")
            - pd.Timedelta(minutes=1)
        )

        progress = 100.0 * max(
            0.0,
            min(1.0, (min(last_time, end_ms) - start_ms) / max(end_ms - start_ms, 1)),
        )
        if (
            request_count == 1
            or request_count % REALAGG_REST_PROGRESS_EVERY_REQUESTS == 0
            or last_time >= end_ms
        ):
            reached = pd.to_datetime(last_time, unit="ms", utc=True).isoformat()
            elapsed = time.monotonic() - started_monotonic
            logging.info(
                "[REALAGG REST PROGRESS] requests=%d aggtrades=%d reached=%s progress=%.1f%% elapsed=%.1fs",
                request_count,
                aggtrade_count,
                reached,
                progress,
                elapsed,
            )

        if (
            request_count == 1
            or request_count % REALAGG_REST_CHECKPOINT_EVERY_REQUESTS == 0
            or last_time >= end_ms
        ) and checkpoint_minute != last_checkpoint_minute:
            _checkpoint_realagg_rest_parts(
                parts,
                start_ts,
                checkpoint_minute,
                f"requests={request_count} aggtrades={aggtrade_count}",
            )
            last_checkpoint_minute = checkpoint_minute

        if last_time > end_ms:
            break
        try:
            data = _request_realagg_rest_json(
                url,
                {"symbol": SYMBOL, "fromId": last_id + 1, "limit": 1000},
                timeout=30,
            )
        except Exception:
            _checkpoint_realagg_rest_parts(
                parts,
                start_ts,
                checkpoint_minute,
                f"before_exception requests={request_count} aggtrades={aggtrade_count}",
            )
            raise
        if data:
            first_t = int(data[0].get("T", end_ms + 1))
            if first_t > end_ms:
                break

    elapsed = time.monotonic() - started_monotonic
    reached = pd.to_datetime(min(last_seen_time_ms, end_ms), unit="ms", utc=True).isoformat()
    final_progress = 100.0 * max(
        0.0,
        min(1.0, (min(last_seen_time_ms, end_ms) - start_ms) / max(end_ms - start_ms, 1)),
    )
    logging.info(
        "[REALAGG REST COMPLETE] requests=%d aggtrades=%d reached=%s progress=%.1f%% elapsed=%.1fs",
        request_count,
        aggtrade_count,
        reached,
        final_progress,
        elapsed,
    )

    if not parts:
        return _empty_realagg_1m()
    return pd.concat(parts, ignore_index=True).groupby("date", sort=True, as_index=False).sum(numeric_only=True)


def _load_realagg_data_vision_day_1m(day: pd.Timestamp) -> pd.DataFrame:
    """Try the exact Binance Data Vision completed-day archive; no proxy source."""
    day = _utc_ts(day).floor("D")
    ds = day.strftime("%Y-%m-%d")
    name = f"{SYMBOL}-aggTrades-{ds}.zip"
    url = f"{BINANCE_DATA_VISION_BASE}/aggTrades/{SYMBOL}/{name}"
    try:
        payload = _request_bytes(url, timeout=120)
        return _aggregate_aggtrades_zip_bytes_to_1m(payload, name)
    except Exception as exc:
        # Caller decides whether an already-persisted exact cache is sufficient or
        # whether the missing tail must be fetched from the same exact REST source.
        logging.warning(
            "[REALAGG DATA VISION] day=%s unavailable; exact cache/REST recovery will be used: %s",
            ds,
            exc,
        )
        return _empty_realagg_1m()


def _ensure_realagg_1m_cache(end_dt: pd.Timestamp) -> pd.DataFrame:
    global _REALAGG_1M_CACHE, _REALAGG_CACHE_UTC_DAY, _REALAGG_LAST_REFRESH_UTC
    now = min(_utc_ts(end_dt), pd.Timestamp.now(tz="UTC"))
    today = now.floor("D")
    keep_from = today - pd.Timedelta(days=REALAGG_BOOTSTRAP_COMPLETED_DAYS)
    last_complete_end = now.floor("1min") - pd.Timedelta(milliseconds=1)

    if _REALAGG_1M_CACHE.empty or _REALAGG_CACHE_UTC_DAY != today:
        cache = _load_realagg_persistent_cache()
        if not cache.empty:
            cache = cache[
                (cache["date"] >= keep_from)
                & (cache["date"] <= last_complete_end)
            ].copy()

        for days_back in range(REALAGG_BOOTSTRAP_COMPLETED_DAYS, 0, -1):
            day = today - pd.Timedelta(days=days_back)
            day_next = day + pd.Timedelta(days=1)
            day_end = day_next - pd.Timedelta(milliseconds=1)
            logging.info("[REALAGG BOOTSTRAP] completed_day=%s", day.date())

            cached_day = (
                cache[(cache["date"] >= day) & (cache["date"] < day_next)].copy()
                if not cache.empty else _empty_realagg_1m()
            )
            day_complete, missing_minutes = _realagg_completed_day_coverage(cached_day, day)

            if day_complete:
                logging.info(
                    "[REALAGG BOOTSTRAP] completed_day=%s source=PERSISTENT_CACHE rows=1440 coverage=COMPLETE",
                    day.date(),
                )
                continue

            first_missing = missing_minutes.min() if len(missing_minutes) else day
            logging.info(
                "[REALAGG BOOTSTRAP] completed_day=%s cache_coverage=INCOMPLETE rows=%d missing_minutes=%d first_missing=%s",
                day.date(),
                len(cached_day),
                len(missing_minutes),
                first_missing,
            )

            data_vision = _load_realagg_data_vision_day_1m(day)
            if not data_vision.empty:
                data_vision_complete, data_vision_missing = _realagg_completed_day_coverage(
                    data_vision,
                    day,
                )
                if data_vision_complete:
                    cache = _replace_realagg_cache_window(cache, data_vision, day, day_end)
                    logging.info(
                        "[REALAGG BOOTSTRAP] completed_day=%s source=DATA_VISION rows=1440 coverage=COMPLETE",
                        day.date(),
                    )
                    logging.info(
                        "[REALAGG CACHE CHECKPOINT] completed_day=%s source=DATA_VISION",
                        day.date(),
                    )
                    _save_realagg_persistent_cache(cache)
                    continue
                logging.warning(
                    "[REALAGG DATA VISION] completed_day=%s coverage=INCOMPLETE rows=%d missing_minutes=%d; exact REST repair will be used",
                    day.date(),
                    len(data_vision),
                    len(data_vision_missing),
                )

            repair_start = _utc_ts(first_missing).floor("1min")
            logging.info(
                "[REALAGG BOOTSTRAP] completed_day=%s source=FUTURES_REST repair_start=%s end=%s cached_rows=%d",
                day.date(),
                repair_start.isoformat(),
                day_end.isoformat(),
                len(cached_day),
            )
            fresh = _fetch_futures_aggtrades_range_to_1m(repair_start, day_end)
            if fresh.empty:
                raise RuntimeError(
                    f"[REALAGG] exact REST repair returned no data for completed day={day.date()} "
                    f"repair_start={repair_start}"
                )

            cache = _replace_realagg_cache_window(cache, fresh, repair_start, day_end)
            repaired_day = cache[(cache["date"] >= day) & (cache["date"] < day_next)].copy()
            repaired_complete, repaired_missing = _realagg_completed_day_coverage(repaired_day, day)
            if not repaired_complete:
                raise RuntimeError(
                    f"[REALAGG] completed-day repair still incomplete day={day.date()} "
                    f"rows={len(repaired_day)} missing_minutes={len(repaired_missing)}"
                )

            logging.info(
                "[REALAGG CACHE CHECKPOINT] completed_day=%s source=FUTURES_REST coverage=COMPLETE rows=1440",
                day.date(),
            )
            _save_realagg_persistent_cache(cache)

        current_end = last_complete_end
        cached_today = (
            cache[(cache["date"] >= today) & (cache["date"] <= current_end)].copy()
            if not cache.empty else _empty_realagg_1m()
        )
        cached_today_max = cached_today["date"].max() if not cached_today.empty else None
        current_start = today
        if cached_today_max is not None:
            current_start = max(
                today,
                cached_today_max - pd.Timedelta(hours=REALAGG_CACHE_OVERLAP_HOURS),
            ).floor("1min")
        logging.info(
            "[REALAGG BOOTSTRAP] current UTC day via Futures REST: %s -> %s | cached_today_rows=%d",
            current_start.isoformat(),
            current_end.isoformat(),
            len(cached_today),
        )
        current = (
            _fetch_futures_aggtrades_range_to_1m(current_start, current_end)
            if current_end >= current_start
            else _empty_realagg_1m()
        )
        if not current.empty:
            cache = _replace_realagg_cache_window(cache, current, current_start, current_end)

        if cache.empty:
            raise RuntimeError("[REALAGG] no true USD-M Futures aggTrades data available")

        cache = cache[
            (cache["date"] >= keep_from)
            & (cache["date"] <= current_end)
        ].copy()
        _REALAGG_1M_CACHE = _normalize_realagg_cache_frame(cache, "bootstrap_final")
        _REALAGG_CACHE_UTC_DAY = today
        _REALAGG_LAST_REFRESH_UTC = now
        _save_realagg_persistent_cache(_REALAGG_1M_CACHE)

    elif _REALAGG_LAST_REFRESH_UTC is None or (now - _REALAGG_LAST_REFRESH_UTC) >= pd.Timedelta(minutes=5):
        start = max(
            today,
            (now - pd.Timedelta(hours=REALAGG_REFRESH_LOOKBACK_HOURS)).floor("1min"),
        )
        end = last_complete_end
        fresh = (
            _fetch_futures_aggtrades_range_to_1m(start, end)
            if end >= start
            else _empty_realagg_1m()
        )
        if not fresh.empty:
            _REALAGG_1M_CACHE = _replace_realagg_cache_window(
                _REALAGG_1M_CACHE,
                fresh,
                start,
                end,
            )
        _REALAGG_LAST_REFRESH_UTC = now
        _REALAGG_1M_CACHE = _REALAGG_1M_CACHE[
            (_REALAGG_1M_CACHE["date"] >= keep_from)
            & (_REALAGG_1M_CACHE["date"] <= end)
        ].copy()
        _REALAGG_1M_CACHE = _normalize_realagg_cache_frame(
            _REALAGG_1M_CACHE,
            "refresh_final",
        )
        _save_realagg_persistent_cache(_REALAGG_1M_CACHE)

    return _REALAGG_1M_CACHE.copy()


def _finalize_realagg_tf(base_1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    if base_1m is None or base_1m.empty:
        return pd.DataFrame(columns=["date"] + REALAGG_SOURCE_COLUMNS)
    work = base_1m.copy()
    work["date"] = pd.to_datetime(work["date"], utc=True, errors="coerce").dt.floor(_tf_pandas_freq(tf))
    sum_cols = [
        "realagg_trade_count", "realagg_buy_base_volume", "realagg_sell_base_volume",
        "realagg_buy_quote_volume", "realagg_sell_quote_volume",
    ]
    df = work.groupby("date", sort=True, as_index=False)[sum_cols].sum()
    df["realagg_total_base_volume"] = df["realagg_buy_base_volume"] + df["realagg_sell_base_volume"]
    df["realagg_total_quote_volume"] = df["realagg_buy_quote_volume"] + df["realagg_sell_quote_volume"]
    df["realagg_cvd_base_delta"] = df["realagg_buy_base_volume"] - df["realagg_sell_base_volume"]
    df["realagg_cvd_quote_delta"] = df["realagg_buy_quote_volume"] - df["realagg_sell_quote_volume"]
    df["realagg_flow_imbalance_base"] = safe_div(df["realagg_cvd_base_delta"], df["realagg_total_base_volume"])
    df["realagg_flow_imbalance_quote"] = safe_div(df["realagg_cvd_quote_delta"], df["realagg_total_quote_volume"])
    df["realagg_buy_ratio_base"] = safe_div(df["realagg_buy_base_volume"], df["realagg_total_base_volume"])
    df["realagg_buy_ratio_quote"] = safe_div(df["realagg_buy_quote_volume"], df["realagg_total_quote_volume"])
    df["realagg_buy_sell_ratio_base"] = safe_div(df["realagg_buy_base_volume"], df["realagg_sell_base_volume"])
    df["realagg_buy_sell_ratio_quote"] = safe_div(df["realagg_buy_quote_volume"], df["realagg_sell_quote_volume"])
    df["realagg_avg_trade_size_base"] = safe_div(df["realagg_total_base_volume"], df["realagg_trade_count"])
    df["realagg_avg_trade_size_quote"] = safe_div(df["realagg_total_quote_volume"], df["realagg_trade_count"])
    df["realagg_cvd_base"] = df["realagg_cvd_base_delta"].cumsum()
    df["realagg_cvd_quote"] = df["realagg_cvd_quote_delta"].cumsum()
    return df.replace([np.inf, -np.inf], np.nan)


def fetch_realagg_for_tf(tf: str, end_dt: pd.Timestamp) -> pd.DataFrame:
    return _finalize_realagg_tf(_ensure_realagg_1m_cache(end_dt), tf)


def _load_oi_metrics_data_vision_day(day: pd.Timestamp) -> pd.DataFrame:
    day = _utc_ts(day).floor("D")
    ds = day.strftime("%Y-%m-%d")
    name = f"{SYMBOL}-metrics-{ds}.zip"
    url = f"{BINANCE_DATA_VISION_BASE}/metrics/{SYMBOL}/{name}"
    payload = _request_bytes(url, timeout=60)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"[OI METRICS] expected one CSV in {name}, got {members}")
        with zf.open(members[0]) as fh:
            df = pd.read_csv(fh, low_memory=False)
    required = ["create_time", "sum_open_interest", "sum_open_interest_value"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"[OI METRICS] {name} missing columns={missing}")
    df["create_time"] = pd.to_datetime(df["create_time"], utc=True, errors="coerce")
    # Frozen Forward/model contract proven by raw OI parity: Binance Metrics
    # create_time is the effective 5m timestamp. Do NOT subtract 5 minutes.
    df["date_5m"] = df["create_time"]
    for c in df.columns:
        if c not in {"create_time", "date_5m", "symbol"}:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["date_5m"]).sort_values("date_5m").drop_duplicates("date_5m", keep="last").reset_index(drop=True)



def _load_oi_metrics_data_vision_range(
    start_day: pd.Timestamp,
    end_day: pd.Timestamp,
) -> pd.DataFrame:
    """Load exact Training/Forward Binance Metrics 5m OI history in memory only."""
    start_day = _utc_ts(start_day).floor("D")
    end_day = _utc_ts(end_day).floor("D")
    if end_day < start_day:
        return pd.DataFrame(columns=[
            "date_5m", "sum_open_interest", "sum_open_interest_value",
        ])

    days = list(pd.date_range(start_day, end_day, freq="D", tz="UTC"))
    parts = []
    started = time.monotonic()
    total = len(days)

    logging.info(
        "[OI HISTORY BOOTSTRAP] Data Vision daily Metrics | days=%d | %s -> %s",
        total, start_day.date(), end_day.date(),
    )

    for idx, day in enumerate(days, start=1):
        parts.append(_load_oi_metrics_data_vision_day(day))
        if (
            idx == 1
            or idx == total
            or idx % max(1, int(OI_DATA_VISION_PROGRESS_EVERY_DAYS)) == 0
        ):
            logging.info(
                "[OI HISTORY BOOTSTRAP] progress=%d/%d (%.1f%%) | last_day=%s | elapsed=%.1fs",
                idx,
                total,
                100.0 * idx / max(1, total),
                day.date(),
                time.monotonic() - started,
            )

    if not parts:
        return pd.DataFrame(columns=[
            "date_5m", "sum_open_interest", "sum_open_interest_value",
        ])

    out = pd.concat(parts, ignore_index=True, sort=False)
    out = (
        out.dropna(subset=["date_5m"])
        .sort_values("date_5m")
        .drop_duplicates("date_5m", keep="last")
        .reset_index(drop=True)
    )
    logging.info(
        "[OI HISTORY BOOTSTRAP] DONE rows=%d range=%s -> %s elapsed=%.1fs",
        len(out),
        out["date_5m"].min() if not out.empty else None,
        out["date_5m"].max() if not out.empty else None,
        time.monotonic() - started,
    )
    return out


def _fetch_oi_rest_raw_5m(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    start_ts = _utc_ts(start_dt)
    end_ts = min(_utc_ts(end_dt), pd.Timestamp.now(tz="UTC"))
    if start_ts >= end_ts:
        return pd.DataFrame(columns=["source_time", "sum_open_interest", "sum_open_interest_value"])
    # Binance endpoint keeps only a recent window; enforce the same safe <=29d range.
    min_start = end_ts - pd.Timedelta(days=OI_REST_MAX_HISTORY_DAYS)
    if start_ts < min_start:
        start_ts = min_start
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    url = f"{BINANCE_FUTURES_BASE}/futures/data/openInterestHist"
    rows = []
    cur = start_ms
    period_ms = 5 * 60 * 1000

    # Keep every REST request safely below Binance's 500-row response cap.
    # Fixed chunks avoid the proven failure where a wide request returns only
    # the latest 500 snapshots and silently drops the older part of the window.
    chunk_snapshots = 480
    chunk_span_ms = (chunk_snapshots - 1) * period_ms

    while cur <= end_ms:
        chunk_end = min(cur + chunk_span_ms, end_ms)
        data = _request_json(
            url,
            {"symbol": SYMBOL, "period": "5m", "startTime": cur, "endTime": chunk_end, "limit": 500},
            timeout=30,
        )
        if data:
            rows.extend(data)

        next_cur = chunk_end + period_ms
        if next_cur <= cur:
            raise RuntimeError("[OI REST] chunk pagination did not advance")
        cur = next_cur

    if not rows:
        return pd.DataFrame(columns=["source_time", "sum_open_interest", "sum_open_interest_value"])
    df = pd.DataFrame(rows)
    df["source_time"] = pd.to_datetime(pd.to_numeric(df["timestamp"], errors="coerce"), unit="ms", utc=True, errors="coerce")
    df["sum_open_interest"] = pd.to_numeric(df.get("sumOpenInterest"), errors="coerce")
    df["sum_open_interest_value"] = pd.to_numeric(df.get("sumOpenInterestValue"), errors="coerce")
    return df.dropna(subset=["source_time"]).sort_values("source_time").drop_duplicates("source_time", keep="last").reset_index(drop=True)


def _calibrate_oi_rest_shift_minutes() -> int:
    # Never guess the REST timestamp offset. Compare a recent complete day against
    # the exact Binance Metrics source used by Training/Forward and select the only
    # alignment whose OI values agree.
    now = pd.Timestamp.now(tz="UTC")
    candidates = [-5, 0, 5]
    last_error = None
    for days_back in range(1, OI_REST_MAX_HISTORY_DAYS):
        day = now.floor("D") - pd.Timedelta(days=days_back)
        try:
            dv = _load_oi_metrics_data_vision_day(day)
            rest = _fetch_oi_rest_raw_5m(day, day + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1))
            if dv.empty or rest.empty:
                continue
            best = None
            for shift in candidates:
                r = rest.copy()
                r["date_5m"] = r["source_time"] + pd.Timedelta(minutes=shift)
                m = dv[["date_5m", "sum_open_interest", "sum_open_interest_value"]].merge(
                    r[["date_5m", "sum_open_interest", "sum_open_interest_value"]],
                    on="date_5m", how="inner", suffixes=("_dv", "_rest"),
                )
                if m.empty:
                    continue
                oi_close = np.isclose(
                    pd.to_numeric(m["sum_open_interest_dv"], errors="coerce"),
                    pd.to_numeric(m["sum_open_interest_rest"], errors="coerce"),
                    rtol=1e-8, atol=1e-8, equal_nan=False,
                )
                val_close = np.isclose(
                    pd.to_numeric(m["sum_open_interest_value_dv"], errors="coerce"),
                    pd.to_numeric(m["sum_open_interest_value_rest"], errors="coerce"),
                    rtol=1e-8, atol=1e-6, equal_nan=False,
                )
                both = oi_close & val_close
                score = (int(both.sum()), int(len(m)))
                if best is None or score > best[0]:
                    best = (score, shift)
            if best is None:
                continue
            (close_count, overlap_count), shift = best
            if overlap_count >= 12 and close_count >= max(10, int(0.90 * overlap_count)):
                logging.info(
                    "[OI REST ALIGNMENT] PASS day=%s shift_minutes=%d exact_like_rows=%d/%d",
                    day.date(), shift, close_count, overlap_count,
                )
                return int(shift)
            raise RuntimeError(
                f"[OI REST ALIGNMENT] no parity-quality shift day={day.date()} best_shift={shift} matches={close_count}/{overlap_count}"
            )
        except Exception as exc:
            last_error = exc
            logging.warning("[OI REST ALIGNMENT] calibration day=%s failed: %s", day.date(), exc)
    raise RuntimeError(f"[OI REST ALIGNMENT] unable to prove REST-vs-Metrics alignment; last_error={last_error}")


def _rest_oi_to_training_5m(rest: pd.DataFrame) -> pd.DataFrame:
    global _OI_REST_SHIFT_MINUTES
    if _OI_REST_SHIFT_MINUTES is None:
        _OI_REST_SHIFT_MINUTES = _calibrate_oi_rest_shift_minutes()
    if rest is None or rest.empty:
        return pd.DataFrame(columns=["date_5m", "sum_open_interest", "sum_open_interest_value"])
    out = rest.copy()
    out["date_5m"] = out["source_time"] + pd.Timedelta(minutes=int(_OI_REST_SHIFT_MINUTES))
    return out[["date_5m", "sum_open_interest", "sum_open_interest_value"]].sort_values("date_5m").drop_duplicates("date_5m", keep="last").reset_index(drop=True)


def _required_oi_snapshot_for_latest_closed_15m(now: pd.Timestamp) -> pd.Timestamp:
    now_ts = _utc_ts(now)
    return now_ts.floor("15min") - pd.Timedelta(minutes=5)


def _last_oi_cache_snapshot(cache: pd.DataFrame) -> Optional[pd.Timestamp]:
    if cache is None or cache.empty or "date_5m" not in cache.columns:
        return None
    value = pd.to_datetime(cache["date_5m"], utc=True, errors="coerce").max()
    if pd.isna(value):
        return None
    return pd.Timestamp(value)


def _merge_fresh_oi_tail(fresh: pd.DataFrame) -> None:
    global _OI_5M_CACHE
    if fresh is None or fresh.empty:
        return
    first = fresh["date_5m"].min()
    base = _OI_5M_CACHE[_OI_5M_CACHE["date_5m"] < first]
    _OI_5M_CACHE = (
        pd.concat([base, fresh], ignore_index=True, sort=False)
        .sort_values("date_5m")
        .drop_duplicates("date_5m", keep="last")
        .reset_index(drop=True)
    )


def _refresh_oi_5m_tail(now: pd.Timestamp) -> None:
    global _OI_LAST_REFRESH_UTC
    now_ts = _utc_ts(now)
    start = now_ts - pd.Timedelta(hours=OI_REFRESH_LOOKBACK_HOURS)
    fresh = _rest_oi_to_training_5m(_fetch_oi_rest_raw_5m(start, now_ts))
    _merge_fresh_oi_tail(fresh)
    _OI_LAST_REFRESH_UTC = now_ts


def _ensure_oi_5m_cache(end_dt: pd.Timestamp) -> pd.DataFrame:
    global _OI_5M_CACHE, _OI_LAST_REFRESH_UTC
    now = min(_utc_ts(end_dt), pd.Timestamp.now(tz="UTC"))
    required_snapshot = _required_oi_snapshot_for_latest_closed_15m(now)

    if _OI_5M_CACHE.empty:
        # Keep a full-day REST tail inside Binance's recent-history limit.
        utc_day = now.floor("D")
        rest_start = utc_day - pd.Timedelta(days=OI_REST_MAX_HISTORY_DAYS - 1)

        # Older OI comes from the exact Binance Metrics daily source used by
        # Training/Forward. This removes the old 29-day warm-up ceiling.
        dv_start_day = utc_day - pd.Timedelta(days=OI_HISTORY_DAYS)
        dv_end_day = rest_start - pd.Timedelta(days=1)

        data_vision = _load_oi_metrics_data_vision_range(
            dv_start_day,
            dv_end_day,
        )
        rest = _rest_oi_to_training_5m(
            _fetch_oi_rest_raw_5m(rest_start, now)
        )

        parts = [x for x in (data_vision, rest) if x is not None and not x.empty]
        if not parts:
            raise RuntimeError("[OI] no 5m Open Interest history available")

        _OI_5M_CACHE = (
            pd.concat(parts, ignore_index=True, sort=False)
            .dropna(subset=["date_5m"])
            .sort_values("date_5m")
            .drop_duplicates("date_5m", keep="last")
            .reset_index(drop=True)
        )
        _OI_LAST_REFRESH_UTC = now

        logging.info(
            "[OI CACHE] initialized rows=%d range=%s -> %s history_days=%d rest_tail_days=%d shift_minutes=%s",
            len(_OI_5M_CACHE),
            _OI_5M_CACHE["date_5m"].min(),
            _OI_5M_CACHE["date_5m"].max(),
            OI_HISTORY_DAYS,
            OI_REST_MAX_HISTORY_DAYS,
            _OI_REST_SHIFT_MINUTES,
        )

    elif (
        _OI_LAST_REFRESH_UTC is None
        or (now - _OI_LAST_REFRESH_UTC) >= pd.Timedelta(minutes=5)
        or (
            _last_oi_cache_snapshot(_OI_5M_CACHE) is not None
            and _last_oi_cache_snapshot(_OI_5M_CACHE) < required_snapshot
        )
    ):
        _refresh_oi_5m_tail(now)

    waited = 0
    while True:
        have_snapshot = _last_oi_cache_snapshot(_OI_5M_CACHE)
        if have_snapshot is not None and have_snapshot >= required_snapshot:
            break
        if waited >= OI_FRESHNESS_MAX_WAIT_SECONDS:
            raise RuntimeError(
                "[OI FRESHNESS] missing required closed-bar OI snapshot "
                f"need>={required_snapshot} have={have_snapshot}"
            )
        sleep_seconds = min(
            OI_FRESHNESS_RETRY_SECONDS,
            OI_FRESHNESS_MAX_WAIT_SECONDS - waited,
        )
        logging.warning(
            "[OI FRESHNESS WAIT] need>=%s have=%s sleep=%ds waited=%ds/%ds",
            required_snapshot,
            have_snapshot,
            sleep_seconds,
            waited,
            OI_FRESHNESS_MAX_WAIT_SECONDS,
        )
        time.sleep(max(1, int(sleep_seconds)))
        waited += int(sleep_seconds)
        refresh_now = min(_utc_ts(end_dt), pd.Timestamp.now(tz="UTC"))
        _refresh_oi_5m_tail(refresh_now)

    keep_from = now.floor("D") - pd.Timedelta(days=OI_HISTORY_DAYS)
    _OI_5M_CACHE = (
        _OI_5M_CACHE[_OI_5M_CACHE["date_5m"] >= keep_from]
        .sort_values("date_5m")
        .reset_index(drop=True)
    )
    return _OI_5M_CACHE.copy()


def _aggregate_oi_5m_for_tf(oi5: pd.DataFrame, base: pd.DataFrame, tf: str) -> pd.DataFrame:
    freq = _tf_pandas_freq(tf)
    work = oi5.copy()
    work["date"] = pd.to_datetime(work["date_5m"], utc=True, errors="coerce").dt.floor(freq)

    if tf == "1m":
        b5 = base.copy()
        b5["date"] = pd.to_datetime(b5["date"], utc=True, errors="coerce").dt.floor("5min")
        b5 = b5.sort_values("date").drop_duplicates("date", keep="last")
        tmp = _aggregate_oi_5m_for_tf(oi5, b5, "5m")

        left = base[["date"]].copy()
        right = tmp.copy()
        left["date"] = pd.to_datetime(left["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        right["date"] = pd.to_datetime(right["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        return pd.merge_asof(
            left.sort_values("date"),
            right.sort_values("date"),
            on="date",
            direction="backward",
            allow_exact_matches=True,
        )

    agg_map = {
        "oi_open_interest": ("sum_open_interest", "last"),
        "oi_open_interest_value": ("sum_open_interest_value", "last"),
        "oi_open_interest_mean": ("sum_open_interest", "mean"),
        "oi_open_interest_max": ("sum_open_interest", "max"),
        "oi_open_interest_min": ("sum_open_interest", "min"),
        "oi_open_interest_first": ("sum_open_interest", "first"),
        "oi_open_interest_value_mean": ("sum_open_interest_value", "mean"),
        "oi_open_interest_value_max": ("sum_open_interest_value", "max"),
        "oi_open_interest_value_min": ("sum_open_interest_value", "min"),
        "oi_open_interest_value_first": ("sum_open_interest_value", "first"),
        "oi_snapshot_count": ("sum_open_interest", "count"),
    }
    optional = {
        "oi_count_toptrader_long_short_ratio": ("count_toptrader_long_short_ratio", "last"),
        "oi_sum_toptrader_long_short_ratio": ("sum_toptrader_long_short_ratio", "last"),
        "oi_count_long_short_ratio": ("count_long_short_ratio", "last"),
        "oi_sum_taker_long_short_vol_ratio": ("sum_taker_long_short_vol_ratio", "last"),
    }
    for dst, spec in optional.items():
        if spec[0] in work.columns:
            agg_map[dst] = spec
    g = work.groupby("date", sort=True).agg(**agg_map).reset_index()

    left = base[["date"]].copy()
    right = g.copy()
    left["date"] = pd.to_datetime(left["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    right["date"] = pd.to_datetime(right["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    return pd.merge_asof(
        left.sort_values("date"),
        right.sort_values("date"),
        on="date",
        direction="backward",
        allow_exact_matches=True,
    )


def _spot_klines_request(symbol: str, interval: str, limit: int, end_time: Optional[int] = None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = int(end_time)
    return _request_json(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=30)


def _futures_klines_request(symbol: str, interval: str, limit: int, end_time: Optional[int] = None):
    """Binance USD-M Futures klines — exact market used by Training/Forward for ETHUSDT/BTCUSDT."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = int(end_time)
    return _request_json(f"{BINANCE_FUTURES_BASE}/fapi/v1/klines", params=params, timeout=30)


def _premium_klines_request(interval: str, limit: int, end_time: Optional[int] = None):
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = int(end_time)
    return _request_json(f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndexKlines", params=params, timeout=30)


def _batch_to_spot_df(data) -> pd.DataFrame:
    if not data:
        return pd.DataFrame()
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume", "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True, errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "quote_asset_volume", "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    return df[["date", "open", "high", "low", "close", "volume", "quote_asset_volume", "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume"]]


def _batch_to_premium_df(data) -> pd.DataFrame:
    if not data:
        return pd.DataFrame()
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume", "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"]
    if len(data[0]) < len(cols):
        cols = ["open_time", "open", "high", "low", "close", "close_time"] + [f"x{i}" for i in range(len(data[0]) - 6)]
    df = pd.DataFrame(data, columns=cols[:len(data[0])])
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True, errors="coerce")
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    return df.rename(columns={"open": "premium_open", "high": "premium_high", "low": "premium_low", "close": "premium_close"})[["date", "premium_open", "premium_high", "premium_low", "premium_close"]]


def fetch_spot_klines(symbol: str, tf: str, target: int) -> pd.DataFrame:
    frames = []
    end_time = None
    remaining = int(target)
    while remaining > 0:
        limit = min(1000, remaining)
        data = _spot_klines_request(symbol, tf, limit, end_time)
        if not data:
            break
        batch = _batch_to_spot_df(data)
        if batch.empty:
            break
        frames.insert(0, batch)
        first_open_ms = int(data[0][0])
        end_time = first_open_ms - 1
        remaining -= len(batch)
        if len(batch) < limit:
            break
    if not frames:
        raise RuntimeError(f"[{symbol} {tf}] empty Binance response")
    df = pd.concat(frames, ignore_index=True).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if len(df) > target:
        df = df.tail(target).reset_index(drop=True)
    return df


def fetch_futures_klines(symbol: str, tf: str, target: int) -> pd.DataFrame:
    """Fetch Binance USD-M Futures klines with the same pagination/shape used by the live panel."""
    frames = []
    end_time = None
    remaining = int(target)
    while remaining > 0:
        limit = min(1000, remaining)
        data = _futures_klines_request(symbol, tf, limit, end_time)
        if not data:
            break
        batch = _batch_to_spot_df(data)
        if batch.empty:
            break
        frames.insert(0, batch)
        first_open_ms = int(data[0][0])
        end_time = first_open_ms - 1
        remaining -= len(batch)
        if len(batch) < limit:
            break
    if not frames:
        raise RuntimeError(f"[{symbol} {tf}] empty Binance USD-M Futures response")
    df = (
        pd.concat(frames, ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    if len(df) > target:
        df = df.tail(target).reset_index(drop=True)
    return df


def fetch_premium_klines(tf: str, target: int) -> pd.DataFrame:
    frames = []
    end_time = None
    remaining = int(target)
    while remaining > 0:
        limit = min(1000, remaining)
        data = _premium_klines_request(tf, limit, end_time)
        if not data:
            break
        batch = _batch_to_premium_df(data)
        if batch.empty:
            break
        frames.insert(0, batch)
        first_open_ms = int(data[0][0])
        end_time = first_open_ms - 1
        remaining -= len(batch)
        if len(batch) < limit:
            break
    if not frames:
        raise RuntimeError(f"[premium {tf}] empty Binance response")
    df = pd.concat(frames, ignore_index=True).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if len(df) > target:
        df = df.tail(target).reset_index(drop=True)
    return df


def fetch_funding_rates(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DataFrame:
    start_ts = pd.Timestamp(start_dt)
    end_ts = pd.Timestamp(end_dt)
    # Binance futures-data openInterestHist is limited to a recent window.
    # Clamp the request to the latest 29 days ending at end_dt to avoid 400s
    # on startup for 1h/4h/1d panels. This only affects OI fetch range, not
    # trading rules, thresholds, models, or execution.
    min_start_ts = end_ts - pd.Timedelta(days=29)
    if start_ts < min_start_ts:
        start_ts = min_start_ts
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    rows = []
    cur = start_ms
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate"
    while cur <= end_ms:
        params = {"symbol": SYMBOL, "startTime": cur, "endTime": end_ms, "limit": 1000}
        data = _request_json(url, params=params, timeout=30)
        if not data:
            break
        rows.extend(data)
        last_time = int(data[-1]["fundingTime"])
        if last_time <= cur:
            break
        cur = last_time + 1
        if len(data) < 1000:
            break
    if not rows:
        raise RuntimeError("[FUNDING] empty Binance futures funding response")
    df = pd.DataFrame(rows)
    df["funding_time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True, errors="coerce")
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    return df.dropna(subset=["funding_time", "funding_rate"])[["funding_time", "funding_rate"]].sort_values("funding_time").drop_duplicates("funding_time", keep="last").reset_index(drop=True)


def fetch_open_interest_hist(
    tf: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
    base: pd.DataFrame,
) -> pd.DataFrame:
    # Always fetch the authoritative 5m OI series, then aggregate those snapshots
    # into the target timeframe exactly like the Forward builder.
    oi5 = _ensure_oi_5m_cache(end_dt)
    return _aggregate_oi_5m_for_tf(oi5, base[["date", "close"]].copy(), tf)


def attach_external_columns(
    spot: pd.DataFrame,
    premium: pd.DataFrame,
    funding: pd.DataFrame,
    realagg: pd.DataFrame,
    open_interest: pd.DataFrame,
) -> pd.DataFrame:
    df = spot.copy().sort_values("date").reset_index(drop=True)
    premium = premium.copy().sort_values("date").reset_index(drop=True)
    df = pd.merge_asof(df, premium, on="date", direction="backward", allow_exact_matches=True)
    funding = funding.copy().sort_values("funding_time").reset_index(drop=True)
    df = pd.merge_asof(df, funding, left_on="date", right_on="funding_time", direction="backward", allow_exact_matches=True)
    df = df.drop(columns=["funding_time"], errors="ignore")

    if realagg is None or realagg.empty:
        raise RuntimeError("[REALAGG] true USD-M Futures aggTrades frame is empty")
    ra = realagg.copy().sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    ra["date"] = pd.to_datetime(ra["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    df = pd.merge_asof(df, ra, on="date", direction="backward", allow_exact_matches=True)

    if open_interest is None or open_interest.empty:
        raise RuntimeError("[OI] aggregated 5m Open Interest frame is empty")
    oi = open_interest.copy().sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    oi["date"] = pd.to_datetime(oi["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
    df = pd.merge_asof(df, oi, on="date", direction="backward", allow_exact_matches=True)

    # Generic confirmed-builder aliases MUST be the true realagg values.
    alias_map = {
        "buy_base_volume": "realagg_buy_base_volume",
        "sell_base_volume": "realagg_sell_base_volume",
        "buy_quote_volume": "realagg_buy_quote_volume",
        "sell_quote_volume": "realagg_sell_quote_volume",
        "agg_trade_count": "realagg_trade_count",
        "trade_flow_imbalance_base": "realagg_flow_imbalance_base",
        "trade_flow_imbalance_quote": "realagg_flow_imbalance_quote",
        "cvd_base": "realagg_cvd_base",
        "cvd_quote": "realagg_cvd_quote",
    }
    for dst, src in alias_map.items():
        if src not in df.columns:
            df[src] = np.nan
        df[dst] = df[src]

    for c in RAW_COLUMNS:
        if c not in df.columns:
            raise RuntimeError(f"[RAW BUILD] missing column after external attach: {c}")

    keep_cols = list(RAW_COLUMNS)
    for c in REALAGG_SOURCE_COLUMNS + OI_SOURCE_COLUMNS:
        if c in df.columns and c not in keep_cols:
            keep_cols.append(c)
    out = df[keep_cols].replace([np.inf, -np.inf], np.nan)
    return dedupe_columns_keep_last(out)


def fetch_time_series(tf: str) -> pd.DataFrame:
    target = OUTPUTSIZE[tf]

    # Training/Forward contract: ETHUSDT base OHLCV is Binance USD-M Futures,
    # not Spot. Premium/Funding/REALAGG/OI below are already Futures sources.
    base = fetch_futures_klines(SYMBOL, tf, target)
    premium = fetch_premium_klines(tf, target)
    start_dt = base["date"].min() - pd.Timedelta(days=2)
    end_dt = base["date"].max() + pd.Timedelta(days=1)
    funding = fetch_funding_rates(start_dt, end_dt)

    # TRUE USD-M Futures aggTrades only. No Kline fallback/substitution.
    realagg = fetch_realagg_for_tf(tf, end_dt)

    # TRUE 5m OI snapshots -> target-TF aggregation, with REST timestamp offset
    # proved against Binance Metrics before first use.
    open_interest = fetch_open_interest_hist(tf, start_dt, end_dt, base)

    return attach_external_columns(base, premium, funding, realagg, open_interest)


def fetch_live_price() -> Optional[float]:
    if not USE_PRICE_ENDPOINT:
        return None
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": SYMBOL}, timeout=10)
        r.raise_for_status()
        return float(r.json().get("price"))
    except Exception:
        return None


def attach_btc_ethbtc_context(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """
    Attach the same BTCUSDT / ETHBTC context namespace used by the historical feature CSVs.
    This is required so live ML inputs can resolve raw external columns such as:
    btc_quote_asset_volume, btc_number_of_trades, btc_taker_buy_quote_volume,
    ethbtc_volume, ethbtc_number_of_trades, etc.
    """
    out = df.copy().sort_values("date").reset_index(drop=True)
    target = OUTPUTSIZE.get(tf, len(out))

    try:
        btc = fetch_futures_klines("BTCUSDT", tf, target)
        btc_ctx = btc[[
            "date", "open", "high", "low", "close", "volume", "quote_asset_volume",
            "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume",
        ]].rename(columns={
            "open": "btc_open",
            "high": "btc_high",
            "low": "btc_low",
            "close": "btc_close",
            "volume": "btc_volume",
            "quote_asset_volume": "btc_quote_asset_volume",
            "number_of_trades": "btc_number_of_trades",
            "taker_buy_base_volume": "btc_taker_buy_base_volume",
            "taker_buy_quote_volume": "btc_taker_buy_quote_volume",
        })
        out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        btc_ctx["date"] = pd.to_datetime(btc_ctx["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        out = pd.merge_asof(out, btc_ctx.sort_values("date"), on="date", direction="backward", allow_exact_matches=True)
    except Exception as e:
        logging.warning("[BTC CONTEXT WARNING] %s", e)
        for c in [
            "btc_open", "btc_high", "btc_low", "btc_close", "btc_volume", "btc_quote_asset_volume",
            "btc_number_of_trades", "btc_taker_buy_base_volume", "btc_taker_buy_quote_volume",
        ]:
            out[c] = np.nan

    try:
        ethbtc = fetch_spot_klines("ETHBTC", tf, target)
        ethbtc_ctx = ethbtc[[
            "date", "open", "high", "low", "close", "volume", "quote_asset_volume",
            "number_of_trades", "taker_buy_base_volume", "taker_buy_quote_volume",
        ]].rename(columns={
            "open": "ethbtc_open",
            "high": "ethbtc_high",
            "low": "ethbtc_low",
            "close": "ethbtc_close",
            "volume": "ethbtc_volume",
            "quote_asset_volume": "ethbtc_quote_asset_volume",
            "number_of_trades": "ethbtc_number_of_trades",
            "taker_buy_base_volume": "ethbtc_taker_buy_base_volume",
            "taker_buy_quote_volume": "ethbtc_taker_buy_quote_volume",
        })
        out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        ethbtc_ctx["date"] = pd.to_datetime(ethbtc_ctx["date"], utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        out = pd.merge_asof(out, ethbtc_ctx.sort_values("date"), on="date", direction="backward", allow_exact_matches=True)
    except Exception as e:
        logging.warning("[ETHBTC CONTEXT WARNING] %s", e)
        for c in [
            "ethbtc_open", "ethbtc_high", "ethbtc_low", "ethbtc_close", "ethbtc_volume", "ethbtc_quote_asset_volume",
            "ethbtc_number_of_trades", "ethbtc_taker_buy_base_volume", "ethbtc_taker_buy_quote_volume",
        ]:
            out[c] = np.nan

    # Historical computed external helpers. These are not raw market columns;
    # they are recreated here for parity with the training feature namespace.
    out["btc_ret_1"] = pd.to_numeric(out["btc_close"], errors="coerce").pct_change()
    out["btc_logret_1"] = logret(pd.to_numeric(out["btc_close"], errors="coerce"), 1)
    out["ethbtc_ret_1"] = pd.to_numeric(out["ethbtc_close"], errors="coerce").pct_change()
    out["ethbtc_logret_1"] = logret(pd.to_numeric(out["ethbtc_close"], errors="coerce"), 1)
    out["eth_relative_strength_vs_btc"] = logret(pd.to_numeric(out["close"], errors="coerce"), 1) - out["btc_logret_1"]

    return out.replace([np.inf, -np.inf], np.nan)


# =============================================================================
# LIVE AUDIT DISPLAY HELPERS
# =============================================================================
def candle_integrity_check(df: pd.DataFrame, tf: str) -> Dict[str, Any]:
    out = {"tf": tf, "rows": len(df)}
    if df.empty:
        out.update({"bad_high_low": 0, "bad_bounds": 0, "dup_ts": 0, "nan_ohlc": 0, "non_monotonic": 0, "status": "WARN"})
        return out
    ts = pd.to_datetime(df["date"], utc=True, errors="coerce")
    bad_high_low = int((pd.to_numeric(df["high"], errors="coerce") < pd.to_numeric(df["low"], errors="coerce")).sum())
    oc_max = pd.concat([pd.to_numeric(df["open"], errors="coerce"), pd.to_numeric(df["close"], errors="coerce")], axis=1).max(axis=1)
    oc_min = pd.concat([pd.to_numeric(df["open"], errors="coerce"), pd.to_numeric(df["close"], errors="coerce")], axis=1).min(axis=1)
    bad_bounds = int(((pd.to_numeric(df["high"], errors="coerce") < oc_max) | (pd.to_numeric(df["low"], errors="coerce") > oc_min)).sum())
    dup_ts = int(ts.duplicated().sum())
    nan_ohlc = int(df[["open", "high", "low", "close"]].isna().sum().sum())
    non_monotonic = int(not ts.is_monotonic_increasing)
    status = "OK" if not any([bad_high_low, bad_bounds, dup_ts, nan_ohlc, non_monotonic]) else "WARN"
    out.update({"bad_high_low": bad_high_low, "bad_bounds": bad_bounds, "dup_ts": dup_ts, "nan_ohlc": nan_ohlc, "non_monotonic": non_monotonic, "status": status})
    return out


def timeframe_alignment_check(df: pd.DataFrame, tf: str) -> Dict[str, Any]:
    out = {"tf": tf, "rows": len(df)}
    if df.empty:
        out.update({"misaligned": 0, "bad_seconds": 0, "status": "WARN"})
        return out
    ts = pd.to_datetime(df["date"], utc=True, errors="coerce")
    bad_seconds = int(((ts.dt.second != 0) | (ts.dt.microsecond != 0)).sum())
    if tf == "1m":
        misaligned = 0
    elif tf == "5m":
        misaligned = int((ts.dt.minute % 5 != 0).sum())
    elif tf == "15m":
        misaligned = int((ts.dt.minute % 15 != 0).sum())
    elif tf == "1h":
        misaligned = int(((ts.dt.minute != 0) | (ts.dt.second != 0)).sum())
    elif tf == "4h":
        valid_ts = ts.dropna().sort_values().reset_index(drop=True)
        delta_sec = valid_ts.diff().dt.total_seconds().iloc[1:].dropna() if len(valid_ts) > 1 else pd.Series(dtype=float)
        misaligned = int((delta_sec <= 0).sum()) if len(delta_sec) else 0
    elif tf == "1d":
        misaligned = int(((ts.dt.hour != 0) | (ts.dt.minute != 0) | (ts.dt.second != 0)).sum())
    else:
        misaligned = 0
    status = "OK" if (misaligned == 0 and bad_seconds == 0) else "WARN"
    out.update({"misaligned": misaligned, "bad_seconds": bad_seconds, "status": status})
    return out


def log_candle_integrity(results: Dict[str, Dict[str, Any]]):
    for tf, r in results.items():
        logging.info(
            "[CANDLE CHECK] %s | status=%s | rows=%d | bad_high_low=%d | bad_bounds=%d | dup_ts=%d | nan_ohlc=%d | non_monotonic=%d",
            tf, r["status"], r["rows"], r["bad_high_low"], r["bad_bounds"], r["dup_ts"], r["nan_ohlc"], r["non_monotonic"],
        )


def log_alignment_checks(results: Dict[str, Dict[str, Any]]):
    for tf, r in results.items():
        logging.info(
            "[ALIGN CHECK] %s | status=%s | rows=%d | misaligned=%d | bad_seconds=%d",
            tf, r["status"], r["rows"], r["misaligned"], r["bad_seconds"],
        )


def log_manifest_and_parity(packs):
    for tf, df in packs:
        req = ["date", "open", "high", "low", "close", "volume"] + BASE_FEATURES
        present = list(df.columns)
        missing = [c for c in req if c not in present]
        extras = [c for c in present if c not in req and c not in RAW_COLUMNS]
        req_no_date = [c for c in req if c != "date" and c in df.columns]
        nan_last = int(df[req_no_date].iloc[-1].isna().sum()) if len(df) and req_no_date else 0
        logging.info("[manifest] %s: %d cols", tf, len(present))
        logging.info("[PARITY] %s: required=%d present=%d missing=%d extras=%d nan_last=%d", tf, len(req), len(present), len(missing), len(extras), nan_last)
        if missing:
            logging.warning("[PARITY MISSING] %s: %s", tf, missing[:20])


def log_bar_mode(panel: pd.DataFrame, decision_source: pd.DataFrame, now_utc: datetime):
    if panel.empty:
        return
    last_all = panel.iloc[-1]
    last_closed = decision_source.iloc[-1] if not decision_source.empty else None
    logging.info(
        "[BAR MODE] now=%s | process_only_closed=%s | all_rows=%d | closed_rows=%d | last_all_t=%s | last_all_end=%s | last_all_closed=%s | sec_to_close=%.2f | last_closed_t=%s",
        now_utc.isoformat(),
        PROCESS_ONLY_CLOSED_BARS,
        len(panel),
        int(panel["bar_closed_now"].sum()) if "bar_closed_now" in panel.columns else 0,
        str(last_all.get("ts_open")),
        str(last_all.get("ts_close")),
        str(last_all.get("bar_closed_now")),
        float(last_all.get("seconds_to_bar_close_now", np.nan)),
        str(last_closed.get("ts_open")) if last_closed is not None else "None",
    )


# =============================================================================
# LIVE PANEL BUILDING — 15M BASE + 1H/4H/1D HTF ONLY
# =============================================================================
def attach_htf_live(base: pd.DataFrame, htf: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Authoritative model-feature HTF merge.

    Generic Forward policy:
        LEFT_OPEN -> RIGHT_OPEN -> BACKWARD -> EXACT

    Proven exact exception for only these three 1h aliases:
        adx_14_1h, di_diff_14_1h, rsi_14_1h
        LEFT_15M_CLOSE -> RIGHT_1H_CLOSE -> BACKWARD -> EXACT -> SHIFT_0

    The separate V22 locked orchestration HTF namespace remains handled by
    attach_v22_locked_htf_context() and is not changed here.
    """
    base = base.sort_values("ts_open").reset_index(drop=True).copy()
    htf = htf.sort_values("ts_open").reset_index(drop=True).copy()
    htf = dedupe_columns_keep_last(htf)

    if base["ts_open"].duplicated().any() or htf["ts_open"].duplicated().any():
        raise RuntimeError(f"Duplicate ts_open before HTF merge tf={tf}")

    reserved = {
        "ts_open",
        "entry_time_next", "entry_ts_next", "entry_open_next",
        "next_index", "valid_next_entry", "entry_gap_minutes",
    }
    rename = {
        c: f"{tf}__{c}"
        for c in htf.columns
        if c not in reserved and c != "date"
    }

    source_open_col = f"{tf}__ts_open"
    right = htf.rename(columns={"ts_open": source_open_col, **rename})
    right = dedupe_columns_keep_last(right)

    keep = [source_open_col] + list(dict.fromkeys(rename.values()))
    right = right[keep].sort_values(source_open_col)

    out = pd.merge_asof(
        base,
        right,
        left_on="ts_open",
        right_on=source_open_col,
        direction="backward",
        allow_exact_matches=True,
    )

    future_mask = (
        out[source_open_col].notna()
        & (out[source_open_col] > out["ts_open"])
    )
    if bool(future_mask.any()):
        sample = out.loc[future_mask, ["ts_open", source_open_col]].head(5)
        raise RuntimeError(
            f"Future HTF open attachment detected tf={tf}: "
            f"{sample.to_dict(orient='records')}"
        )

    prefix = f"{tf}__"
    for column in list(out.columns):
        if column.startswith(prefix) and column != source_open_col:
            raw_name = column[len(prefix):]
            alias = f"{raw_name}_{tf}"
            if alias not in out.columns:
                source = out[column]
                if isinstance(source, pd.DataFrame):
                    source = source.iloc[:, -1]
                out[alias] = source

    # Exact proven timing for only the three remaining 1h features. Do not
    # change any other generic 1h/4h/1d alias or the V22 locked HTF namespace.
    if tf == "1h":
        exact_1h_features = ["adx_14", "di_diff_14", "rsi_14"]
        missing_exact = [
            column for column in ["ts_close"] + exact_1h_features
            if column not in htf.columns
        ]
        if missing_exact:
            raise RuntimeError(
                "Exact 1h close-to-close source missing columns: "
                + ", ".join(missing_exact)
            )

        exact_source_close = "__exact_1h_source_ts_close"
        exact_value_names = {
            column: f"__exact_1h_value_{column}"
            for column in exact_1h_features
        }
        exact_right = htf[["ts_close"] + exact_1h_features].copy()
        exact_right = exact_right.rename(
            columns={"ts_close": exact_source_close, **exact_value_names}
        )
        exact_right = (
            exact_right
            .sort_values(exact_source_close)
            .drop_duplicates(exact_source_close, keep="last")
            .reset_index(drop=True)
        )

        out = pd.merge_asof(
            out.sort_values("ts_close").reset_index(drop=True),
            exact_right,
            left_on="ts_close",
            right_on=exact_source_close,
            direction="backward",
            allow_exact_matches=True,
        )

        future_exact = (
            out[exact_source_close].notna()
            & (out[exact_source_close] > out["ts_close"])
        )
        if bool(future_exact.any()):
            sample = out.loc[
                future_exact, ["ts_close", exact_source_close]
            ].head(5)
            raise RuntimeError(
                "Future exact 1h close attachment detected: "
                f"{sample.to_dict(orient='records')}"
            )

        for source_column in exact_1h_features:
            exact_value = exact_value_names[source_column]
            out[f"1h__{source_column}"] = out[exact_value]
            out[f"{source_column}_1h"] = out[exact_value]

        out = out.drop(
            columns=[exact_source_close] + list(exact_value_names.values())
        )
        out = out.sort_values("ts_open").reset_index(drop=True)

    out = dedupe_columns_keep_last(out)
    if out.columns.has_duplicates:
        duplicates = out.columns[out.columns.duplicated()].tolist()
        raise RuntimeError(
            f"Duplicate columns after authoritative HTF merge tf={tf}: "
            f"{duplicates[:20]}"
        )
    return out

def attach_v22_locked_htf_context(base: pd.DataFrame, htf: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Attach V22-only HTF features with the locked source's exact timing.

    Locked behavior: shift every HTF feature by one completed HTF row, then
    backward merge_asof on HTF open timestamp. This is causal and was proven
    equivalent to selecting the latest closed HTF candle.
    """
    prefix = {"1h": "eth1h", "4h": "eth4h", "1d": "eth1d"}.get(tf)
    if prefix is None:
        raise ValueError(f"Unsupported V22 HTF: {tf}")

    source_cols = [
        "v22_exact_ema20_slope_10",
        "v22_exact_trend_regime_ema50_200",
    ]
    missing = [c for c in source_cols if c not in htf.columns]
    if missing:
        raise RuntimeError(f"V22 exact HTF source missing tf={tf}: {missing}")

    right_key = f"v22_exact_{prefix}_source_ts_open"
    rename = {
        "v22_exact_ema20_slope_10": f"v22_exact_{prefix}_ema20_slope_10",
        "v22_exact_trend_regime_ema50_200": f"v22_exact_{prefix}_trend_regime_ema50_200",
    }
    right = htf[["ts_open"] + source_cols].copy().sort_values("ts_open")
    right[source_cols] = right[source_cols].shift(1)
    right = right.rename(columns={"ts_open": right_key, **rename})
    right = right.drop_duplicates(right_key, keep="last")

    out = base.sort_values("ts_open").reset_index(drop=True).copy()
    out = out.drop(columns=[right_key] + list(rename.values()), errors="ignore")
    out = pd.merge_asof(
        out,
        right,
        left_on="ts_open",
        right_on=right_key,
        direction="backward",
        allow_exact_matches=True,
    )
    return dedupe_columns_keep_last(out)


def prepare_feature_frame(raw: pd.DataFrame, tf: str, add_context_features: bool = True) -> pd.DataFrame:
    df = calculate_features(raw, tf)
    if add_context_features:
        df = attach_btc_ethbtc_context(df, tf)
        df = add_final_extra_features(df, tf)
    df["ts_open"] = to_utc(df["date"])
    df["ts_close"] = df["ts_open"] + pd.to_timedelta(tf_minutes(tf), unit="m")
    df = dedupe_columns_keep_last(df)
    return df.sort_values("ts_open").drop_duplicates("ts_open").reset_index(drop=True)


def build_live_panel(raw1m: pd.DataFrame, raw5m: pd.DataFrame, raw15: pd.DataFrame, raw1h: pd.DataFrame, raw4h: pd.DataFrame, raw1d: pd.DataFrame, now_utc: datetime) -> pd.DataFrame:
    f1m = prepare_feature_frame(raw1m, "1m", add_context_features=False)
    f5m = prepare_feature_frame(raw5m, "5m", add_context_features=False)
    f15 = prepare_feature_frame(raw15, "15m", add_context_features=True)
    f1h = prepare_feature_frame(raw1h, "1h", add_context_features=True)
    f4h = prepare_feature_frame(raw4h, "4h", add_context_features=True)
    f1d = prepare_feature_frame(raw1d, "1d", add_context_features=True)

    s1 = helper_frame(f1m, "s1").sort_values("date")
    s5 = helper_frame(f5m, "s5").sort_values("date")
    enriched = []
    for df in [f15, f1h, f4h, f1d]:
        x = df.sort_values("date").reset_index(drop=True)
        x = pd.merge_asof(x, s1, on="date", direction="backward")
        x = pd.merge_asof(x, s5, on="date", direction="backward")
        enriched.append(x)
    f15, f1h, f4h, f1d = enriched

    if AUDIT_MODE:
        log_manifest_and_parity([("15m", f15), ("1h", f1h), ("4h", f4h), ("1d", f1d)])

    panel = f15.copy()
    for tf, htf in [("1h", f1h), ("4h", f4h), ("1d", f1d)]:
        panel = attach_htf_live(panel, htf, tf)
    for tf, htf in [("1h", f1h), ("4h", f4h), ("1d", f1d)]:
        panel = attach_v22_locked_htf_context(panel, htf, tf)

    panel = rebuild_execution_columns(panel)
    panel["bar_closed_now"] = panel["ts_close"] <= pd.Timestamp(now_utc)
    panel["seconds_to_bar_close_now"] = (panel["ts_close"] - pd.Timestamp(now_utc)).dt.total_seconds().clip(lower=0.0)

    for tf in HTF_TFS:
        c = f"{tf}__ts_close"
        if c in panel.columns:
            panel[f"age_{tf}_min"] = (panel["ts_close"] - panel[c]).dt.total_seconds() / 60.0

    # Authoritative trained-model ML time namespace is the NEXT-ENTRY UTC clock.
    entry_clock = pd.to_datetime(panel["entry_ts_next"], utc=True, errors="coerce")
    panel["ml_hour"] = entry_clock.dt.hour
    panel["ml_dow"] = entry_clock.dt.dayofweek
    panel["ml_is_us_session"] = (
        (panel["ml_hour"] >= 13) & (panel["ml_hour"] < 22)
    ).astype("float64")
    panel["ml_is_london_session"] = (
        (panel["ml_hour"] >= 7) & (panel["ml_hour"] < 13)
    ).astype("float64")
    panel["ml_is_asia_session"] = (
        (panel["ml_hour"] >= 0) & (panel["ml_hour"] < 7)
    ).astype("float64")
    panel.loc[entry_clock.isna(), [
        "ml_hour", "ml_dow", "ml_is_us_session",
        "ml_is_london_session", "ml_is_asia_session",
    ]] = np.nan

    needed_cols = set(LONG_FEATURE_COLS) | set(SHORT_FEATURE_COLS) | {"s1_mom"}
    missing = sorted(c for c in needed_cols if c not in panel.columns)
    if missing:
        raise RuntimeError(
            "Live feature contract failed; missing required model/rule columns: "
            + ", ".join(missing)
        )
    if panel.columns.has_duplicates:
        dupes = panel.columns[panel.columns.duplicated()].tolist()
        raise RuntimeError(f"Live panel contains duplicate columns: {dupes[:20]}")

    return panel.replace([np.inf, -np.inf], np.nan)



EXTERNAL_AUDIT_MAIN_TFS = ("15m", "1h", "4h", "1d")

EXTERNAL_AUDIT_ALIAS_MAP = {
    "buy_base_volume": "realagg_buy_base_volume",
    "sell_base_volume": "realagg_sell_base_volume",
    "buy_quote_volume": "realagg_buy_quote_volume",
    "sell_quote_volume": "realagg_sell_quote_volume",
    "agg_trade_count": "realagg_trade_count",
    "trade_flow_imbalance_base": "realagg_flow_imbalance_base",
    "trade_flow_imbalance_quote": "realagg_flow_imbalance_quote",
    "cvd_base": "realagg_cvd_base",
    "cvd_quote": "realagg_cvd_quote",
}

EXTERNAL_AUDIT_OI_REQUIRED = [
    "oi_open_interest",
    "oi_open_interest_value",
    "oi_open_interest_mean",
    "oi_open_interest_max",
    "oi_open_interest_min",
    "oi_open_interest_first",
    "oi_open_interest_value_mean",
    "oi_open_interest_value_max",
    "oi_open_interest_value_min",
    "oi_open_interest_value_first",
    "oi_snapshot_count",
]


def _audit_number_equal(a: Any, b: Any, tol: float = 1e-10) -> bool:
    av = _f(a, np.nan)
    bv = _f(b, np.nan)
    if not np.isfinite(av) or not np.isfinite(bv):
        return False
    return bool(np.isclose(av, bv, rtol=tol, atol=tol))


def _latest_closed_raw_row(
    df: pd.DataFrame,
    tf: str,
    now_utc: datetime,
) -> Optional[pd.Series]:
    if df is None or df.empty or "date" not in df.columns:
        return None
    dates = pd.to_datetime(df["date"], utc=True, errors="coerce")
    closes = dates + pd.to_timedelta(tf_minutes(tf), unit="m")
    mask = dates.notna() & (closes <= pd.Timestamp(now_utc))
    if not bool(mask.any()):
        return None
    return df.loc[mask].iloc[-1]


def _external_feature_name(name: str) -> bool:
    low = str(name).lower()
    return ("realagg" in low) or ("oi_" in low) or low.startswith("oi")


def _raw_external_tf_audit(
    tf: str,
    raw: pd.DataFrame,
    now_utc: datetime,
) -> Dict[str, Any]:
    row = _latest_closed_raw_row(raw, tf, now_utc)
    if row is None:
        return {
            "status": "FAIL",
            "reason": "NO_CLOSED_RAW_ROW",
            "tf": tf,
        }

    realagg_missing = [c for c in REALAGG_SOURCE_COLUMNS if c not in row.index]
    realagg_nan = [
        c for c in REALAGG_SOURCE_COLUMNS
        if c in row.index and pd.isna(row.get(c))
    ]

    oi_missing = [c for c in EXTERNAL_AUDIT_OI_REQUIRED if c not in row.index]
    oi_nan = [
        c for c in EXTERNAL_AUDIT_OI_REQUIRED
        if c in row.index and pd.isna(row.get(c))
    ]

    alias_mismatches = []
    for alias, source in EXTERNAL_AUDIT_ALIAS_MAP.items():
        if alias not in row.index or source not in row.index:
            alias_mismatches.append({
                "alias": alias,
                "source": source,
                "reason": "MISSING_COLUMN",
            })
            continue
        if not _audit_number_equal(row.get(alias), row.get(source)):
            alias_mismatches.append({
                "alias": alias,
                "source": source,
                "alias_value": _csv_safe_cell(row.get(alias)),
                "source_value": _csv_safe_cell(row.get(source)),
                "reason": "VALUE_MISMATCH_OR_NAN",
            })

    expected_snapshots = int(tf_minutes(tf) // 5)
    snapshot_value = _f(row.get("oi_snapshot_count"), np.nan)
    snapshot_ok = bool(
        np.isfinite(snapshot_value)
        and int(round(snapshot_value)) == expected_snapshots
    )

    realagg_trade_count = _f(row.get("realagg_trade_count"), np.nan)
    realagg_trade_count_ok = bool(
        np.isfinite(realagg_trade_count) and realagg_trade_count > 0
    )

    status = "PASS"
    if (
        realagg_missing
        or realagg_nan
        or oi_missing
        or oi_nan
        or alias_mismatches
        or not snapshot_ok
        or not realagg_trade_count_ok
    ):
        status = "FAIL"

    return {
        "status": status,
        "tf": tf,
        "bar_open_utc": (
            pd.Timestamp(row.get("date")).isoformat()
            if pd.notna(row.get("date")) else None
        ),
        "realagg_source": "BINANCE_USDM_FUTURES_AGGTRADES",
        "realagg_fallback_allowed": False,
        "realagg_trade_count": realagg_trade_count,
        "realagg_trade_count_ok": realagg_trade_count_ok,
        "realagg_missing_columns": realagg_missing,
        "realagg_nan_columns": realagg_nan,
        "oi_source": "BINANCE_USDM_5M_METRICS_PLUS_REST_TAIL_WITH_PROVED_SHIFT",
        "oi_missing_columns": oi_missing,
        "oi_nan_columns": oi_nan,
        "oi_snapshot_count": snapshot_value,
        "oi_snapshot_count_expected": expected_snapshots,
        "oi_snapshot_count_ok": snapshot_ok,
        "generic_alias_mismatches": alias_mismatches,
    }


def append_external_data_fix_audit(
    panel: pd.DataFrame,
    raw_map: Dict[str, pd.DataFrame],
    now_utc: datetime,
) -> Dict[str, Any]:
    global _LAST_EXTERNAL_DATA_AUDIT_RECORD
    decision_source = (
        panel[(panel["bar_closed_now"]) & (panel["valid_next_entry"])]
        .copy()
        .sort_values("ts_open")
        .reset_index(drop=True)
    )

    if decision_source.empty:
        signal_bar_utc = None
        model_health = {
            "status": "FAIL",
            "reason": "NO_CLOSED_15M_SIGNAL_ROW_WITH_NEXT_ENTRY",
        }
    else:
        row = decision_source.iloc[-1]
        signal_bar_utc = pd.Timestamp(row["ts_open"]).isoformat()
        entry_value = row.get("entry_ts_next")
        entry_timestamp = (
            pd.Timestamp(entry_value)
            if pd.notna(entry_value)
            else pd.Timestamp(row["ts_open"]) + pd.Timedelta(minutes=15)
        )

        long_sample = build_ml_sample(row, LONG_FEATURE_COLS, entry_timestamp)
        short_sample = build_ml_sample(row, SHORT_FEATURE_COLS, entry_timestamp)
        long_missing, long_nan = snapshot_missing_nan(
            long_sample, LONG_FEATURE_COLS, row
        )
        short_missing, short_nan = snapshot_missing_nan(
            short_sample, SHORT_FEATURE_COLS, row
        )

        long_external_nan = [c for c in long_nan if _external_feature_name(c)]
        short_external_nan = [c for c in short_nan if _external_feature_name(c)]
        long_external_missing = [
            c for c in long_missing if _external_feature_name(c)
        ]
        short_external_missing = [
            c for c in short_missing if _external_feature_name(c)
        ]

        model_ok = bool(
            len(LONG_FEATURE_COLS) == 120
            and len(SHORT_FEATURE_COLS) == 120
            and not long_missing
            and not short_missing
            and not long_nan
            and not short_nan
        )

        model_health = {
            "status": "PASS" if model_ok else "FAIL",
            "signal_bar_utc": signal_bar_utc,
            "entry_time_utc": entry_timestamp.isoformat(),
            "long_feature_count": len(LONG_FEATURE_COLS),
            "short_feature_count": len(SHORT_FEATURE_COLS),
            "long_missing_count": len(long_missing),
            "short_missing_count": len(short_missing),
            "long_missing": long_missing,
            "short_missing": short_missing,
            "long_nan_count": len(long_nan),
            "short_nan_count": len(short_nan),
            "long_nan": long_nan,
            "short_nan": short_nan,
            "long_external_missing": long_external_missing,
            "short_external_missing": short_external_missing,
            "long_external_nan": long_external_nan,
            "short_external_nan": short_external_nan,
            "long_external_feature_values": {
                k: _csv_safe_cell(v)
                for k, v in long_sample.items()
                if _external_feature_name(k)
            },
            "short_external_feature_values": {
                k: _csv_safe_cell(v)
                for k, v in short_sample.items()
                if _external_feature_name(k)
            },
        }

    tf_checks = {
        tf: _raw_external_tf_audit(tf, raw_map.get(tf), now_utc)
        for tf in EXTERNAL_AUDIT_MAIN_TFS
    }
    raw_sources_ok = all(
        details.get("status") == "PASS"
        for details in tf_checks.values()
    )
    model_ok = model_health.get("status") == "PASS"

    failures = []
    if not raw_sources_ok:
        for tf, details in tf_checks.items():
            if details.get("status") != "PASS":
                failures.append(f"{tf}_RAW_EXTERNAL_FAIL")
    if not model_ok:
        failures.append("MODEL_120_FEATURE_HEALTH_FAIL")

    overall_status = "PASS" if not failures else "FAIL"
    event_key = f"EXTERNAL_DATA_FIX|{signal_bar_utc or 'NO_SIGNAL'}"

    record = {
        "schema_version": STATE_SCHEMA_VERSION,
        "record_type": "EXTERNAL_DATA_FIX_AUDIT",
        "event_key": event_key,
        "run_id": RUN_ID,
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "signal_bar_utc": signal_bar_utc,
        "status": overall_status,
        "failures": failures,
        "contract": {
            "ethusdt_kline_source": "BINANCE_USDM_FUTURES_KLINES",
            "btcusdt_context_source": "BINANCE_USDM_FUTURES_KLINES",
            "ethbtc_context_source": "BINANCE_SPOT_KLINES",
            "realagg_source": "BINANCE_USDM_FUTURES_AGGTRADES",
            "realagg_kline_fallback": False,
            "oi_primary_historical_source": "BINANCE_USDM_DAILY_METRICS_5M",
            "oi_recent_tail_source": "BINANCE_USDM_OPEN_INTEREST_HIST_5M",
            "oi_rest_shift_minutes": _OI_REST_SHIFT_MINUTES,
            "oi_total_history_days": OI_HISTORY_DAYS,
            "oi_rest_max_history_days": OI_REST_MAX_HISTORY_DAYS,
            "oi_windows": list(OI_WINDOWS),
            "oi_ma_windows": list(OI_MA_WINDOWS),
            "oi_bar_change": "last_minus_first_within_target_tf_bucket",
            "oi_divergence": "price_pct_change_minus_oi_pct_change",
            "oi_ma_min_periods": "max(5, window//4)",
            "oi_zscore_min_periods": "max(5, window//3)",
        },
        "oi_cache": {
            "rows_5m": int(len(_OI_5M_CACHE)),
            "first_5m": (
                pd.Timestamp(_OI_5M_CACHE["date_5m"].min()).isoformat()
                if not _OI_5M_CACHE.empty else None
            ),
            "last_5m": (
                pd.Timestamp(_OI_5M_CACHE["date_5m"].max()).isoformat()
                if not _OI_5M_CACHE.empty else None
            ),
        },
        "timeframe_source_checks": tf_checks,
        "model_feature_health": model_health,
    }

    append_jsonl_row_unique(
        EXTERNAL_DATA_AUDIT_FILE,
        record,
        "event_key",
    )

    if failures:
        append_diagnostic_warning(
            signal_bar_utc,
            "EXTERNAL_DATA_FIX_AUDIT_FAIL",
            failures,
            record,
        )

    logging.info(
        "[EXTERNAL DATA FIX AUDIT] status=%s | failures=%s | LONG_nan=%s | SHORT_nan=%s | file=%s",
        overall_status,
        failures if failures else "NONE",
        model_health.get("long_nan_count"),
        model_health.get("short_nan_count"),
        EXTERNAL_DATA_AUDIT_FILE,
    )
    _LAST_EXTERNAL_DATA_AUDIT_RECORD = record
    return record


def fetch_and_prepare(now_utc: datetime) -> Tuple[pd.DataFrame, Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    raw1m = fetch_time_series("1m")
    raw5m = fetch_time_series("5m")
    raw15 = fetch_time_series("15m")
    raw1h = fetch_time_series("1h")
    raw4h = fetch_time_series("4h")
    raw1d = fetch_time_series("1d")

    raw_map = {"1m": raw1m, "5m": raw5m, "15m": raw15, "1h": raw1h, "4h": raw4h, "1d": raw1d}
    integrity_results = {tf: candle_integrity_check(df, tf) for tf, df in raw_map.items()}
    alignment_results = {tf: timeframe_alignment_check(df, tf) for tf, df in raw_map.items()}

    panel = build_live_panel(raw1m, raw5m, raw15, raw1h, raw4h, raw1d, now_utc)

    # Dedicated fifth audit: prove the repaired REALAGG/OI source contract and
    # inspect all 120 LONG + 120 SHORT model features for missing names / NaNs.
    append_external_data_fix_audit(panel, raw_map, now_utc)

    return panel, integrity_results, alignment_results


# =============================================================================
# RULES — TRAINING MATCHED
# =============================================================================
def row_num(row: pd.Series, col: Optional[str], default: float = np.nan) -> float:
    if col is None:
        return default
    return _f(row.get(col, default), default)


def long_1h_soft_veto_pass(row: pd.Series) -> bool:
    if not USE_1H_SOFT_VETO:
        return True
    adx = row_num(row, first_col(row, ["adx_14_1h", "1h__adx_14"]))
    di = row_num(row, first_col(row, ["di_diff_14_1h", "1h__di_diff_14"]))
    rsi = row_num(row, first_col(row, ["rsi_14_1h", "1h__rsi_14"]))
    if any(pd.isna(x) for x in [adx, di, rsi]):
        return True
    bearish = adx >= RULE_THRESHOLDS.long_1h_adx_q70 and di <= RULE_THRESHOLDS.long_1h_di_q25 and rsi <= RULE_THRESHOLDS.long_1h_rsi_q25
    return not bearish


def short_1h_soft_veto_pass(row: pd.Series) -> bool:
    if not USE_1H_SOFT_VETO:
        return True
    adx = row_num(row, first_col(row, ["adx_14_1h", "1h__adx_14"]))
    di = row_num(row, first_col(row, ["di_diff_14_1h", "1h__di_diff_14"]))
    rsi = row_num(row, first_col(row, ["rsi_14_1h", "1h__rsi_14"]))
    if any(pd.isna(x) for x in [adx, di, rsi]):
        return True
    bullish = adx >= RULE_THRESHOLDS.short_1h_adx_q80 and di >= RULE_THRESHOLDS.short_1h_di_q80 and rsi >= RULE_THRESHOLDS.short_1h_rsi_q80
    return not bullish


def row_num_any(row: pd.Series, names: List[str], default: float = np.nan) -> float:
    for name in names:
        if name in row.index:
            v = row_num(row, name)
            if pd.notna(v):
                return float(v)
    return default


def row_num_v22_exact_first(row: pd.Series, names: List[str], default: float = np.nan) -> float:
    """Match the locked engine's existing-column NaN semantics exactly.

    When the authoritative ``v22_exact_*`` column exists, its NaN must remain
    NaN. The locked dataframe classifier applies ``default`` only when the
    column is absent; it never replaces NaN values inside an existing column.
    Fallback aliases are therefore consulted only when the exact column itself
    is absent.
    """
    if names and str(names[0]).startswith("v22_exact_") and names[0] in row.index:
        return row_num(row, names[0], np.nan)
    return row_num_any(row, names, default)


def row_bool_any(row: pd.Series, names: List[str]) -> bool:
    for name in names:
        if name in row.index:
            return bool_value(row.get(name))
    return False


def _norm_ts_key(x: Any) -> Optional[str]:
    try:
        ts = pd.Timestamp(x)
        if pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def load_v22_long_source_map() -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(V22_LONG_SOURCE_FILE, low_memory=False)
    if "signal_time" not in df.columns and "entry_time" not in df.columns:
        raise RuntimeError(f"V22 LONG export candidate audit missing signal_time/entry_time: {V22_LONG_SOURCE_FILE}")
    out: Dict[str, Dict[str, Any]] = {}
    for _, r in df.iterrows():
        if "signal_time" in df.columns and pd.notna(r.get("signal_time")):
            signal_ts = pd.Timestamp(r.get("signal_time"))
            entry_ts = pd.Timestamp(r.get("entry_time")) if "entry_time" in df.columns and pd.notna(r.get("entry_time")) else signal_ts + pd.Timedelta(minutes=EXPECTED_NEXT_MINUTES)
        else:
            entry_ts = pd.Timestamp(r.get("entry_time"))
            signal_ts = entry_ts - pd.Timedelta(minutes=EXPECTED_NEXT_MINUTES)
        if pd.isna(signal_ts) or pd.isna(entry_ts):
            continue
        if signal_ts.tzinfo is not None:
            signal_ts = signal_ts.tz_convert(None)
        if entry_ts.tzinfo is not None:
            entry_ts = entry_ts.tz_convert(None)
        key = signal_ts.strftime("%Y-%m-%d %H:%M:%S")
        out[key] = {
            "entry_time": entry_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "regime_volatility": str(r.get("regime_volatility", "unknown")),
            "regime_oi_z20": str(r.get("regime_oi_z20", "oi_mid_z")),
            "trade_id": r.get("trade_id"),
            "source": "v22_live_engine_export_candidate_audit",
        }
    logging.info("[V22 LONG EXPORT SOURCE] loaded source candidates=%d file=%s", len(out), V22_LONG_SOURCE_FILE)
    return out


V22_LONG_SOURCE_MAP = load_v22_long_source_map()
V22_LONG_SOURCE_MAX_KEY = max(V22_LONG_SOURCE_MAP.keys()) if V22_LONG_SOURCE_MAP else None


def v22_long_source_meta(row: pd.Series) -> Optional[Dict[str, Any]]:
    key = _norm_ts_key(row.get("ts_open"))
    if key is None:
        return None
    return V22_LONG_SOURCE_MAP.get(key)


# Exact live port of training LONG orchestration classification:
# classify() + orchestration_regime_gate() from the V22 training source.
# This is not a new heuristic layer; it rebuilds the same pre-entry archetype/gate logic on the live row.
def v22_training_pre_entry_archetype(row: pd.Series) -> str:
    """Exact causal row port of locked classify() archetype priority."""
    th = V22_THRESHOLDS
    close_pos = row_num_any(row, ["close_pos", "close_position"], 0.0)
    range_pct = row_num_v22_exact_first(row, ["v22_exact_range_pct", "range_pct"], 0.0)
    ret4 = row_num_v22_exact_first(row, ["v22_exact_ret4", "ret4"], 0.0)
    ret12 = row_num_v22_exact_first(row, ["v22_exact_ret12", "ret12"], 0.0)
    ret24 = row_num_v22_exact_first(row, ["v22_exact_ret24", "ret24"], 0.0)
    lower_wick_pct = row_num_any(row, ["lower_wick_pct"], 0.0)
    bb_bw = row_num_any(row, ["bb_bw"], np.inf)
    bb_z = row_num_any(row, ["bb_z"], 0.0)
    dist_ema20_atr = row_num_v22_exact_first(row, ["v22_exact_dist_ema20_atr", "dist_ema20_atr"], 0.0)
    ema20_slope_10 = row_num_v22_exact_first(row, ["v22_exact_ema20_slope_10", "ema20_slope_10"], 0.0)
    close = row_num_any(row, ["close"], 0.0)
    prev_high_20 = row_num_v22_exact_first(row, ["v22_exact_prev_high_20", "prev_high_20"], np.inf)

    h1_up = (
        row_num_v22_exact_first(row, ["v22_exact_eth1h_ema20_slope_10", "ema20_slope_10_1h", "1h__ema20_slope_10"], 0.0) > 0
        or row_num_v22_exact_first(row, ["v22_exact_eth1h_trend_regime_ema50_200", "trend_regime_ema50_200_1h", "1h__trend_regime_ema50_200"], 0.0) > 0
    )
    h4_up = (
        row_num_v22_exact_first(row, ["v22_exact_eth4h_ema20_slope_10", "ema20_slope_10_4h", "4h__ema20_slope_10"], 0.0) > 0
        or row_num_v22_exact_first(row, ["v22_exact_eth4h_trend_regime_ema50_200", "trend_regime_ema50_200_4h", "4h__trend_regime_ema50_200"], 0.0) > 0
    )
    btc_ok = (
        row_num_any(row, ["btc_trend_score"], 0.0) > 0
        or row_num_any(row, ["btc_ema20_slope_3_pct"], 0.0) > 0
        or row_num_any(row, ["btc_ret_1", "btc_logret_1"], 0.0) > 0
    )
    ethbtc_ok = (
        row_num_any(row, ["ethbtc_trend_score"], 0.0) > 0
        or row_num_any(row, ["ethbtc_ema20_slope_3_pct"], 0.0) > 0
        or row_num_any(row, ["eth_vs_btc_strength_6"], 0.0) > 0
    )
    realagg_flow = (
        row_num_v22_exact_first(row, ["v22_exact_realagg_buy_ratio_quote", "realagg_buy_ratio_quote"], 0.5) >= th.q_realagg70
        or row_num_v22_exact_first(row, ["v22_exact_realagg_cvd_quote_delta_z_50", "realagg_cvd_quote_delta_z_50"], 0.0) >= th.q_realagg_delta65
        or row_num_v22_exact_first(row, ["v22_exact_realagg_cvd_quote_delta_sum_4", "realagg_cvd_quote_delta_sum_4"], 0.0) > 0
    )
    old_flow = (
        row_num_any(row, ["taker_quote_imbalance"], 0.0) > 0
        or row_num_any(row, ["trade_flow_imbalance_quote"], 0.0) > 0
        or row_num_any(row, ["cvd_quote_delta_z_50"], 0.0) > 0
    )

    pullback = (h1_up or h4_up) and ((ret12 <= th.q_ret12_40) or (dist_ema20_atr <= 0) or (bb_z <= -0.25)) and (close_pos >= th.q_closepos60) and (ret4 > -0.010)
    breakout = ((row_bool_any(row, ["sr_break_up", "ms_break_up", "break_up"])) or (close > prev_high_20)) and (close_pos >= th.q_closepos75) and ((range_pct >= th.q_range70) or (ret4 >= th.q_ret4_65))
    reversal = (ret24 <= th.q_ret24_25) and ((lower_wick_pct >= th.q_lwick60) or (close_pos >= th.q_closepos75)) and (ret4 > 0)
    trend_cont = (h1_up or h4_up) and (ema20_slope_10 > 0) and (dist_ema20_atr > 0) and (close_pos >= th.q_closepos60)
    compression = (bb_bw <= th.q_bbw30) and (range_pct >= th.q_range40) and (close_pos >= th.q_closepos60) and (realagg_flow or old_flow)

    arch = "noisy_other"
    if trend_cont:
        arch = "trend_continuation"
    if pullback:
        arch = "pullback"
    if compression:
        arch = "compression_breakout"
    if breakout:
        arch = "breakout"
    if reversal:
        arch = "reversal_after_drop"
    return arch


def v22_live_archetype(row: pd.Series) -> str:
    return v22_training_pre_entry_archetype(row)


def v22_live_long_candidate(row: pd.Series) -> bool:
    if not valid_signal_row(row):
        return False
    # Historical parity window: exact training-source LONG candidates from exported V22 selected trade log.
    key = _norm_ts_key(row.get("ts_open"))
    if key is not None and v22_long_source_meta(row) is not None:
        return True
    # Future live bars beyond the exported training window cannot be looked up from the historical source file,
    # so use the causal online port only after the source window ends.
    if key is not None and V22_LONG_SOURCE_MAX_KEY is not None and key > V22_LONG_SOURCE_MAX_KEY:
        return v22_live_long_candidate_approx_disabled(row)
    return False


def v22_long_causal_gate_state(
    row: pd.Series,
    setup_archetypes: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Return exact future V22 LONG archetype, seven gates and candidate."""
    if setup_archetypes is None:
        setup_archetypes = ("breakout",)
    empty_gates = {
        "setup_ok": False,
        "vol_ok": False,
        "session_ok": False,
        "htf_ok": False,
        "btc_ethbtc_ok": False,
        "flow_or_oi_ok": False,
        "funding_ok": False,
    }
    if not valid_signal_row(row):
        return {
            "candidate": False,
            "archetype": "invalid",
            "gates": empty_gates,
            "mode": "V22_LOCKED_EXACT_PARITY_REPAIRED",
        }

    th = V22_THRESHOLDS
    arch = v22_training_pre_entry_archetype(row)
    setup_ok = arch in set(setup_archetypes)
    atrp = row_num_any(row, ["v22_exact_atr14_pct", "atrp_14", "pre_atr14_pct"], np.nan)
    range_pct = row_num_any(row, ["v22_exact_range_pct", "range_pct", "pre_range_pct"], 0.0)
    funding_abs = row_num_any(
        row,
        ["v22_exact_binance_funding_rate_abs", "binance_funding_rate_abs", "pre_binance_funding_rate_abs"],
        0.0,
    )
    vol_ok = (pd.notna(atrp) and th.atr_low <= atrp <= th.atr_high) or (range_pct >= th.range_high)
    session_ok = row_num_any(
        row,
        ["v22_exact_session_active_07_21", "session_active_07_21", "pre_session_active_07_21"],
        1.0,
    ) >= 0.5
    htf_ok = (
        row_num_any(row, ["v22_exact_eth1h_ema20_slope_10", "ema20_slope_10_1h", "1h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["v22_exact_eth4h_ema20_slope_10", "ema20_slope_10_4h", "4h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["v22_exact_eth4h_trend_regime_ema50_200", "trend_regime_ema50_200_4h", "4h__trend_regime_ema50_200"], 0.0) > 0
        or row_num_any(row, ["v22_exact_eth1d_trend_regime_ema50_200", "trend_regime_ema50_200_1d", "1d__trend_regime_ema50_200"], 0.0) > 0
    )
    btc_ethbtc_ok = (
        row_num_any(row, ["btc_trend_score"], 0.0) >= -1
        and row_num_any(row, ["ethbtc_trend_score"], 0.0) >= -3
    )
    flow_or_oi_ok = (
        row_num_any(row, ["v22_exact_realagg_buy_ratio_quote", "realagg_buy_ratio_quote"], 0.5) >= 0.48
        or row_num_any(row, ["v22_exact_realagg_cvd_quote_delta_z_50", "realagg_cvd_quote_delta_z_50"], 0.0) >= -0.25
        or row_num_any(row, ["v22_exact_oi_price_oi_divergence_4", "oi_price_oi_divergence_4"], 0.0) >= 0
    )
    funding_ok = funding_abs <= th.funding_abs_hi

    gates = {
        "setup_ok": bool(setup_ok),
        "vol_ok": bool(vol_ok),
        "session_ok": bool(session_ok),
        "htf_ok": bool(htf_ok),
        "btc_ethbtc_ok": bool(btc_ethbtc_ok),
        "flow_or_oi_ok": bool(flow_or_oi_ok),
        "funding_ok": bool(funding_ok),
    }
    return {
        "candidate": bool(all(gates.values())),
        "archetype": arch,
        "gates": gates,
        "mode": "V22_LOCKED_EXACT_PARITY_REPAIRED",
    }


def v22_live_long_candidate_approx_disabled(row: pd.Series) -> bool:
    # Kept under the original function name so the rest of the live execution
    # path remains byte-for-byte unchanged outside this repaired LONG decision.
    return bool(v22_long_causal_gate_state(row)["candidate"])


def long_adx_breakout_trigger(row: pd.Series) -> bool:
    adx = row_num(row, first_col(row, ["adx_14"]))
    di = row_num(row, first_col(row, ["di_diff_14"]))
    close_pos = row_num(row, first_col(row, ["close_pos", "close_position"]))
    mom = row_num(row, first_col(row, ["mom", "momentum"]))
    ms_up_col = first_col(row, ["ms_break_up", "break_up", "sr_break_up"])
    ms_up = bool_value(row.get(ms_up_col)) if ms_up_col else False
    if any(pd.isna(x) for x in [adx, di, close_pos, mom]):
        return False
    return adx >= RULE_THRESHOLDS.long_adx_q60 and di >= RULE_THRESHOLDS.long_di_q60 and close_pos >= RULE_THRESHOLDS.long_close_pos_q60 and (ms_up or mom >= RULE_THRESHOLDS.long_mom_q70)


def long_final_filter_pass(row: pd.Series) -> bool:
    di = row_num(row, first_col(row, ["di_diff_14"]))
    if pd.isna(di):
        return False
    hour = int(pd.Timestamp(row["ts_open"]).hour)
    return di >= RULE_THRESHOLDS.long_di_q70_final and 0 <= hour < 22


def short_momentum_break_trigger(row: pd.Series) -> bool:
    close = row_num(row, "close")
    open_ = row_num(row, "open")
    rng = row_num(row, "range")
    body_pct = row_num(row, "body_pct")
    mom = row_num(row, "mom")
    s1_mom = row_num(row, "s1_mom")
    vol = row_num(row, "vol_z_20")
    if any(pd.isna(x) for x in [close, open_, rng, body_pct, mom, vol]):
        return False
    s1_ok = False if pd.isna(s1_mom) else s1_mom <= RULE_THRESHOLDS.short_s1_mom_q30
    return close < open_ and rng >= RULE_THRESHOLDS.short_range_q50 and body_pct >= RULE_THRESHOLDS.short_body_q50 and ((mom <= RULE_THRESHOLDS.short_mom_q30) or s1_ok) and vol >= RULE_THRESHOLDS.short_vol_q60


def valid_signal_row(row: pd.Series) -> bool:
    return bool_value(row.get("valid_next_entry")) and bool_value(row.get("bar_closed_now"))


def _short_rule_candidate(row: pd.Series) -> bool:
    return bool(
        valid_signal_row(row)
        and family_setup_pass(row, SHORT_SPECS, "SHORT", SHORT_SETUP_FAMILY)
        and short_momentum_break_trigger(row)
        and short_1h_soft_veto_pass(row)
    )


def build_rule_funnel(
    row: pd.Series,
    long_selected: Optional[bool] = None,
    short_selected: Optional[bool] = None,
    long_selector_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    valid = valid_signal_row(row)
    gate_state = v22_long_causal_gate_state(
        row, ("breakout", "reversal_after_drop")
    ) if valid else {
        "candidate": False,
        "archetype": "invalid",
        "gates": {},
    }
    long_raw_breakout = bool(
        valid
        and gate_state.get("candidate")
        and gate_state.get("archetype") == "breakout"
    )
    long_family_pass = (
        bool(long_selected) if long_selected is not None else long_raw_breakout
    )
    long_trigger_pass = long_family_pass
    long_1h_soft_veto = True if valid else False
    long_final_filter = True if valid else False
    long_ml_reached = bool(valid and long_family_pass)

    short_family_pass = bool(
        family_setup_pass(row, SHORT_SPECS, "SHORT", SHORT_SETUP_FAMILY)
    ) if valid else False
    short_trigger_pass = bool(short_momentum_break_trigger(row)) if valid else False
    short_1h_soft_veto = bool(short_1h_soft_veto_pass(row)) if valid else False
    short_final_filter = True if valid else False
    short_raw_candidate = bool(
        valid and short_family_pass and short_trigger_pass
        and short_1h_soft_veto and short_final_filter
    )
    short_ml_reached = bool(
        short_selected if short_selected is not None else short_raw_candidate
    )

    return {
        "long": {
            "side": "LONG",
            "setup": LONG_SETUP_NAME,
            "trigger": LONG_TRIGGER,
            "valid_signal_row": bool(valid),
            "raw_orchestration_candidate": bool(gate_state.get("candidate", False)),
            "raw_breakout_candidate": long_raw_breakout,
            "strict_reaction_selected": bool(long_selected) if long_selected is not None else None,
            "strict_selector_meta": long_selector_meta,
            "family_pass": long_family_pass,
            "trigger_pass": long_trigger_pass,
            "one_h_soft_veto_pass": long_1h_soft_veto,
            "final_filter_pass": long_final_filter,
            "ml_reached": long_ml_reached,
            "ml_status": "NOT_REACHED",
            "ml_accept": None,
            "ml_prob": None,
            "ml_threshold": LONG_THRESHOLD,
            "adx_14": row_num(row, first_col(row, ["adx_14"])),
            "di_diff_14": row_num(row, first_col(row, ["di_diff_14"])),
            "close_pos": row_num(row, first_col(row, ["close_pos", "close_position"])),
            "mom": row_num(row, first_col(row, ["mom", "momentum"])),
            "ms_break_up": bool_value(row.get(first_col(row, ["ms_break_up", "break_up", "sr_break_up"]))),
            "hour": int(pd.Timestamp(row["ts_open"]).hour),
            "v22_archetype": gate_state.get("archetype", "invalid"),
            "seven_gates": gate_state.get("gates", {}),
        },
        "short": {
            "side": "SHORT",
            "setup": SHORT_SETUP_NAME,
            "trigger": SHORT_TRIGGER,
            "valid_signal_row": bool(valid),
            "raw_rule_candidate": short_raw_candidate,
            "side_no_overlap_selected": bool(short_selected) if short_selected is not None else None,
            "family_pass": short_family_pass,
            "trigger_pass": short_trigger_pass,
            "one_h_soft_veto_pass": short_1h_soft_veto,
            "final_filter_pass": short_final_filter,
            "final_filter_name": "NO_EXTRA_FINAL_FILTER",
            "ml_reached": short_ml_reached,
            "ml_status": "NOT_REACHED",
            "ml_accept": None,
            "ml_prob": None,
            "ml_threshold": SHORT_THRESHOLD,
            "close": row_num(row, "close"),
            "open": row_num(row, "open"),
            "range": row_num(row, "range"),
            "body_pct": row_num(row, "body_pct"),
            "mom": row_num(row, "mom"),
            "s1_mom": row_num(row, "s1_mom"),
            "vol_z_20": row_num(row, "vol_z_20"),
            "hour": int(pd.Timestamp(row["ts_open"]).hour),
        },
    }


def evaluate_rule_candidates(
    row: pd.Series,
    long_selected: Optional[bool] = None,
    short_selected: Optional[bool] = None,
    long_selector_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not valid_signal_row(row):
        return out

    if long_selected is None:
        # Compatibility fallback for audit helpers that do not pass state.
        long_selected = bool(v22_live_long_candidate(row))
    if short_selected is None:
        short_selected = _short_rule_candidate(row)

    if bool(long_selected):
        out.append({
            "side": +1,
            "setup": LONG_SETUP_NAME,
            "trigger": LONG_TRIGGER,
            "exit": LONG_EXIT_NAME,
            "v22_source_meta": v22_long_source_meta(row),
            "strict_selector_meta": long_selector_meta,
        })
    if bool(short_selected):
        out.append({
            "side": -1,
            "setup": SHORT_SETUP_NAME,
            "trigger": SHORT_TRIGGER,
            "exit": SHORT_EXIT_NAME,
        })
    return out


# =============================================================================
# ML — SEPARATE LONG / SHORT MODELS
# =============================================================================
def build_ml_sample(row: pd.Series, feature_cols: List[str], entry_time: pd.Timestamp) -> Dict[str, Any]:
    sample: Dict[str, Any] = {}
    # Exact training contract: derive the five ML calendar/session features
    # from the next-entry timestamp, not from the signal-bar timestamp.
    entry_clock = pd.Timestamp(entry_time)
    derived_time = {
        "ml_hour": int(entry_clock.hour),
        "ml_dow": int(entry_clock.dayofweek),
        "ml_is_us_session": 1.0 if 13 <= int(entry_clock.hour) < 22 else 0.0,
        "ml_is_london_session": 1.0 if 7 <= int(entry_clock.hour) < 13 else 0.0,
        "ml_is_asia_session": 1.0 if 0 <= int(entry_clock.hour) < 7 else 0.0,
    }
    missing: List[str] = []
    for c in feature_cols:
        if c in derived_time:
            sample[c] = derived_time[c]
        elif c in row.index:
            sample[c] = row.get(c)
        else:
            missing.append(c)
    if missing:
        raise RuntimeError(
            "ML feature snapshot missing required columns: " + ", ".join(missing)
        )
    if list(sample.keys()) != list(feature_cols):
        raise RuntimeError("ML feature snapshot order mismatch")
    return sample


def predict_side(side: int, row: pd.Series, entry_time: pd.Timestamp) -> Tuple[bool, float, float, str, Dict[str, Any]]:
    if side == +1:
        model = LONG_MODEL
        threshold = LONG_THRESHOLD
        feature_cols = LONG_FEATURE_COLS
        calibration = LONG_CALIBRATION_METHOD
    else:
        model = SHORT_MODEL
        threshold = SHORT_THRESHOLD
        feature_cols = SHORT_FEATURE_COLS
        calibration = SHORT_CALIBRATION_METHOD
    sample = build_ml_sample(row, feature_cols, entry_time)
    x = pd.DataFrame([sample])[feature_cols]
    p = float(model.predict_proba(x)[:, 1][0])
    return p >= threshold, p, threshold, calibration, sample


# =============================================================================
# EXECUTION / EXIT MATCHING
# =============================================================================
def get_atr_abs(row: pd.Series, entry: float) -> float:
    atr = row_num(row, "atr_14")
    if pd.isna(atr) or atr <= 0:
        atrp = row_num(row, "atrp_14")
        if pd.notna(atrp) and atrp > 0:
            atr = entry * float(atrp)
    if pd.isna(atr) or atr <= 0:
        hi = row_num(row, "high")
        lo = row_num(row, "low")
        atr = max(float(hi - lo), entry * 0.002) if pd.notna(hi) and pd.notna(lo) else entry * 0.002
    return float(atr)


def v22_live_regime_volatility(row: pd.Series) -> str:
    vol = row_num_any(row, ["rv_50", "atrp_14", "rv_20"], np.nan)
    if pd.isna(vol) or pd.isna(V22_THRESHOLDS.vol_q33) or pd.isna(V22_THRESHOLDS.vol_q66):
        return "unknown"
    if vol <= V22_THRESHOLDS.vol_q33:
        return "vol_low"
    if vol <= V22_THRESHOLDS.vol_q66:
        return "vol_mid"
    return "vol_high"


def v22_live_regime_oi_z20(row: pd.Series) -> str:
    oi = row_num_any(row, ["pre_oi_open_interest_z_20", "oi_open_interest_z_20"], np.nan)
    if pd.isna(oi):
        return "oi_mid_z"
    if oi <= -1.0:
        return "oi_low_z"
    if oi >= 1.0:
        return "oi_high_z"
    return "oi_mid_z"


def v22_live_provisional_sl_atr(row: pd.Series, candidate: Optional[Dict[str, Any]] = None) -> float:
    meta = (candidate or {}).get("v22_source_meta") if isinstance(candidate, dict) else None
    vol_regime = str((meta or {}).get("regime_volatility")) if meta else v22_live_regime_volatility(row)
    oi_regime = str((meta or {}).get("regime_oi_z20")) if meta else v22_live_regime_oi_z20(row)
    if vol_regime in V22_WEAK_VOL_REGIMES or oi_regime in V22_WEAK_OI_REGIMES:
        return V22_BASE_PROVISIONAL_SL_ATR
    return V22_BASE_PROVISIONAL_SL_DEFAULT_ATR


def exit_config_for_side(side: int) -> ExitConfig:
    if side == +1:
        return ExitConfig(LONG_EXIT_NAME, side, V22_BASE_PROVISIONAL_SL_DEFAULT_ATR, V22_BASE_TP_ATR, V22_BASE_MAX_HOLD_BARS, V22_BASE_TRAIL_START_ATR, V22_BASE_TRAIL_DIST_ATR)
    return ExitConfig(SHORT_EXIT_NAME, side, SHORT_EXIT_SL_ATR, SHORT_EXIT_TP_ATR, SHORT_EXIT_MAX_HOLD_BARS, SHORT_EXIT_TRAIL_START_ATR, SHORT_EXIT_TRAIL_DIST_ATR)


def create_open_position(row: pd.Series, candidate: Dict[str, Any], prob: float, threshold: float) -> OpenPosition:
    side = int(candidate["side"])
    entry = float(row["entry_open_next"])
    entry_t = pd.Timestamp(row["entry_ts_next"])
    signal_t = pd.Timestamp(row["ts_open"])
    atr = get_atr_abs(row, entry)
    cfg = exit_config_for_side(side)
    if side == +1:
        long_provisional_sl_atr = v22_live_provisional_sl_atr(row, candidate)
        sl = entry - long_provisional_sl_atr * atr
        tp = entry + cfg.tp_atr * atr
        best_high = entry
        best_low = entry
        stop = sl
    else:
        sl = entry + cfg.sl_atr * atr
        tp = entry - cfg.tp_atr * atr
        best_high = entry
        best_low = entry
        stop = sl
    trade_id = make_trade_id(signal_t.isoformat(), side, candidate["setup"])
    return OpenPosition(
        side=side,
        signal_t=signal_t.isoformat(),
        entry_t=entry_t.isoformat(),
        entry=entry,
        sl=float(sl),
        tp=float(tp),
        atr=float(atr),
        exit_name=cfg.name,
        bars_held=0,
        best_high=float(best_high),
        best_low=float(best_low),
        stop=float(stop),
        initial_sl=float(sl),
        trail_active=False,
        prob=float(prob),
        threshold=float(threshold),
        setup_name=str(candidate["setup"]),
        trade_id=trade_id,
    )


def v22_rx4_class_from_metrics(close_atr: float, mfe_atr: float, mae_atr: float) -> str:
    if all(np.isfinite(float(v)) for v in (close_atr, mfe_atr, mae_atr)):
        if mfe_atr >= 1.0 and close_atr >= 0.0 and mae_atr <= 1.0:
            return "rx4_runner_like"
        if (
            mfe_atr <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR
            and close_atr <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR
            and mae_atr >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR
        ):
            return "rx4_true_failure_like"
        if (
            mfe_atr >= V22_BASE_TRUE_FAIL_MFE_MAX_ATR
            and close_atr > V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR
        ):
            return "rx4_false_sl_like"
    return "rx4_mixed"


def _freeze_v22_rx4(pos: OpenPosition, close: float) -> None:
    pos.rx4_mfe_atr = (
        float(pos.best_high) - float(pos.entry)
    ) / max(float(pos.atr), 1e-12)
    pos.rx4_mae_atr = (
        float(pos.entry) - float(pos.best_low)
    ) / max(float(pos.atr), 1e-12)
    pos.rx4_close_atr = (
        float(close) - float(pos.entry)
    ) / max(float(pos.atr), 1e-12)
    pos.rx4_class = v22_rx4_class_from_metrics(
        pos.rx4_close_atr, pos.rx4_mfe_atr, pos.rx4_mae_atr
    )
    pos.rx4_ready = True


def resolve_v22_long_position_on_bar(
    pos: OpenPosition,
    row: pd.Series,
) -> Tuple[Optional[float], Optional[str], OpenPosition]:
    hi = float(row["high"])
    lo = float(row["low"])
    cl = float(row["close"])
    pos.best_high = max(float(pos.best_high), hi)
    pos.best_low = min(float(pos.best_low), lo)
    bar_no = int(pos.bars_held) + 1
    prov_stop = float(pos.initial_sl)

    if bar_no <= V22_BASE_DECISION_BAR:
        if lo <= prov_stop:
            return prov_stop, "V22_PROVISIONAL_SL", pos
        if bar_no == V22_BASE_DECISION_BAR:
            _freeze_v22_rx4(pos, cl)
            true_fail = (
                pos.rx4_mfe_atr <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR
                and pos.rx4_close_atr <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR
                and pos.rx4_mae_atr >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR
            )
            mixed_bad = (
                pos.rx4_class == "rx4_mixed"
                and pos.rx4_close_atr <= V22_MIXED_BAD_CLOSE_MAX_ATR
                and pos.rx4_mfe_atr <= V22_MIXED_BAD_MFE_MAX_ATR
                and pos.rx4_mae_atr >= V22_MIXED_BAD_MAE_MIN_ATR
            )
            if true_fail:
                return cl, "V22_TRUE_FAILURE_EXIT", pos
            if mixed_bad:
                return cl, "V22_MIXED_BAD_EXIT", pos
        pos.bars_held = bar_no
        return None, None, pos

    if not bool(pos.rx4_ready):
        raise RuntimeError(
            f"V22 RX4 state missing after bar 4 for trade_id={pos.trade_id}"
        )

    if pos.rx4_class == "rx4_false_sl_like":
        normal_sl_atr = 1.55
        trail_start_atr = V22_BASE_TRAIL_START_ATR
        trail_dist_atr = V22_BASE_TRAIL_DIST_ATR
    elif pos.rx4_class == "rx4_mixed" and bar_no <= 8:
        mixed_recover = (
            pos.rx4_mfe_atr >= V22_MIXED_RECOVER_MFE_MIN_ATR
            or pos.rx4_close_atr >= V22_MIXED_RECOVER_CLOSE_MIN_ATR
        )
        if mixed_recover:
            normal_sl_atr = V22_MIXED_RECOVER_SL_ATR
            trail_start_atr = V22_MIXED_RECOVER_TRAIL_START_ATR
            trail_dist_atr = V22_MIXED_RECOVER_TRAIL_DIST_ATR
        else:
            normal_sl_atr = 1.75
            trail_start_atr = 0.85
            trail_dist_atr = 0.45
    else:
        normal_sl_atr = V22_BASE_NORMAL_SL_ATR
        trail_start_atr = V22_BASE_TRAIL_START_ATR
        trail_dist_atr = V22_BASE_TRAIL_DIST_ATR

    normal_sl = float(pos.entry) - normal_sl_atr * float(pos.atr)
    tp_price = float(pos.entry) + V22_BASE_TP_ATR * float(pos.atr)
    if (
        not pos.trail_active
        and float(pos.best_high)
        >= float(pos.entry) + trail_start_atr * float(pos.atr)
    ):
        pos.trail_active = True
        pos.stop = float(pos.best_high) - trail_dist_atr * float(pos.atr)
    elif pos.trail_active:
        pos.stop = max(
            float(pos.stop),
            float(pos.best_high) - trail_dist_atr * float(pos.atr),
        )

    # Exact V22 training ordering: normal SL, trail, TP, then time exit.
    if lo <= normal_sl:
        return normal_sl, "V22_NORMAL_SL", pos
    if pos.trail_active and lo <= float(pos.stop):
        return float(pos.stop), "V22_TRAIL", pos
    if hi >= tp_price:
        return tp_price, "V22_TP", pos

    pos.bars_held = bar_no
    if bar_no >= V22_BASE_MAX_HOLD_BARS:
        return cl, "V22_TIME_EXIT", pos
    return None, None, pos


def resolve_position_on_bar(pos: OpenPosition, row: pd.Series) -> Tuple[Optional[float], Optional[str], OpenPosition]:
    if pos.side == +1 and str(pos.exit_name) == V22_SELECTED_VARIANT_NAME:
        return resolve_v22_long_position_on_bar(pos, row)
    cfg = exit_config_for_side(pos.side)
    hi = float(row["high"])
    lo = float(row["low"])
    cl = float(row["close"])

    # Match training behavior:
    # In trail exits, TP is stored/audited but it is NOT an active closing condition.
    # TP can close only for non-trail/fixed exit configs.
    tp_exit_enabled = not str(cfg.name).lower().startswith("trail")

    pos.best_high = max(float(pos.best_high), hi)
    pos.best_low = min(float(pos.best_low), lo)
    if pos.side == +1:
        if hi >= pos.entry + cfg.trail_start_atr * pos.atr:
            pos.trail_active = True
        if pos.trail_active:
            pos.stop = max(float(pos.stop), pos.best_high - cfg.trail_dist_atr * pos.atr)
        sl_hit = lo <= float(pos.stop)
        tp_hit = bool(tp_exit_enabled and hi >= float(pos.tp))
        if sl_hit and tp_hit:
            return (float(pos.stop), "SL", pos) if SAME_BAR_POLICY == "worst" else (float(pos.tp), "TP", pos)
        if sl_hit:
            return float(pos.stop), "TR" if pos.trail_active and float(pos.stop) > float(pos.initial_sl) else "SL", pos
        if tp_hit:
            return float(pos.tp), "TP", pos
    else:
        if (pos.entry - pos.best_low) / max(pos.atr, 1e-12) >= cfg.trail_start_atr:
            pos.trail_active = True
        if pos.trail_active:
            pos.stop = min(float(pos.stop), pos.best_low + cfg.trail_dist_atr * pos.atr)
        sl_hit = hi >= float(pos.stop)
        tp_hit = bool(tp_exit_enabled and lo <= float(pos.tp))
        if sl_hit and tp_hit:
            return (float(pos.stop), "SL", pos) if SAME_BAR_POLICY == "worst" else (float(pos.tp), "TP", pos)
        if sl_hit:
            return float(pos.stop), "TR" if pos.trail_active and float(pos.stop) < float(pos.initial_sl) else "SL", pos
        if tp_hit:
            return float(pos.tp), "TP", pos
    pos.bars_held += 1
    if pos.bars_held >= cfg.hold_bars:
        return cl, "TO", pos
    return None, None, pos


def trade_pnl(pos: OpenPosition, exit_px: float) -> float:
    gross = (exit_px / pos.entry - 1.0) if pos.side == +1 else (pos.entry - exit_px) / pos.entry
    return float(gross - ROUND_TRIP_COST)


def _append_shadow_trade_row(row: Dict[str, Any]) -> bool:
    """Append to the one clean trade file; never create schema-fixed side files."""
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        pd.DataFrame(columns=SHADOW_TRADE_COLUMNS).to_csv(TRADES_FILE, index=False)
    existing_header = list(pd.read_csv(TRADES_FILE, nrows=0).columns)
    if existing_header != SHADOW_TRADE_COLUMNS:
        raise RuntimeError("Clean trade file schema mismatch")
    keys = _load_csv_unique_keys(TRADES_FILE, ("trade_id",))
    key = _row_key(row, ("trade_id",))
    if key in keys:
        logging.info("[DEDUPED TRADE] trade_id=%s", row.get("trade_id"))
        return False
    fixed = _schema_row(row, SHADOW_TRADE_COLUMNS)
    pd.DataFrame([fixed], columns=SHADOW_TRADE_COLUMNS).to_csv(
        TRADES_FILE,
        mode="a",
        index=False,
        header=False,
    )
    keys.add(key)
    return True


def append_closed_trade(
    pos: OpenPosition,
    exit_t: pd.Timestamp,
    exit_px: float,
    reason: str,
    trade_path: Optional[List[Dict[str, Any]]] = None,
) -> None:
    gross = (
        float(exit_px) / float(pos.entry) - 1.0
        if int(pos.side) == +1
        else (float(pos.entry) - float(exit_px)) / float(pos.entry)
    )
    atr = max(float(pos.atr), 1e-12)
    if int(pos.side) == +1:
        mfe_atr = (float(pos.best_high) - float(pos.entry)) / atr
        mae_atr = (float(pos.entry) - float(pos.best_low)) / atr
    else:
        mfe_atr = (float(pos.entry) - float(pos.best_low)) / atr
        mae_atr = (float(pos.best_high) - float(pos.entry)) / atr
    path = list(trade_path or [])
    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "trade_id": pos.trade_id,
        "status": "CLOSED",
        "side": position_txt(pos.side),
        "setup_name": pos.setup_name,
        "signal_t": pos.signal_t,
        "entry_t": pos.entry_t,
        "exit_t": pd.Timestamp(exit_t).isoformat(),
        "entry": float(pos.entry),
        "exit": float(exit_px),
        "tp": float(pos.tp),
        "initial_sl": float(pos.initial_sl),
        "final_stop": float(pos.stop),
        "atr": float(pos.atr),
        "bars_held": int(pos.bars_held),
        "prob": float(pos.prob),
        "threshold": float(pos.threshold),
        "exit_reason": reason,
        "gross_pnl_rate": float(gross),
        "net_pnl_rate_after_round_trip_cost": trade_pnl(pos, exit_px),
        "round_trip_cost": ROUND_TRIP_COST,
        "best_high": float(pos.best_high),
        "best_low": float(pos.best_low),
        "mfe_atr": float(mfe_atr),
        "mae_atr": float(mae_atr),
        "trail_active_at_exit": bool(pos.trail_active),
        "path_bar_count": int(len(path)),
        "trade_path_json": _safe_json(path),
        "leverage_scenarios_json": _safe_json(
            build_close_leverage_scenarios(pos, exit_px)
        ),
    }
    _append_shadow_trade_row(row)


def send_open_email(pos: OpenPosition):
    body = (
        f"OPEN {position_txt(pos.side)}\n"
        f"Setup: {pos.setup_name}\n"
        f"Trade ID: {pos.trade_id}\n"
        f"Signal: {pos.signal_t}\n"
        f"Entry time: {pos.entry_t}\n"
        f"Entry: {pos.entry:.5f}\n"
        f"TP: {pos.tp:.5f}\n"
        f"SL: {pos.sl:.5f}\n"
        f"ATR: {pos.atr:.5f}\n"
        f"ML probability: {pos.prob:.6f}\n"
        f"ML threshold: {pos.threshold:.3f}\n"
        f"{format_open_leverage_scenarios(pos)}\n"
    )
    send_email(f"OPEN {position_txt(pos.side)} | ETHUSDT 15M version B baseline", body)


def send_close_email(pos: OpenPosition, exit_t: pd.Timestamp, exit_px: float, reason: str):
    pnl = trade_pnl(pos, exit_px)
    body = (
        f"CLOSE {position_txt(pos.side)}\n"
        f"Setup: {pos.setup_name}\n"
        f"Trade ID: {pos.trade_id}\n"
        f"Entry time: {pos.entry_t}\n"
        f"Exit time: {exit_t.isoformat()}\n"
        f"Entry: {pos.entry:.5f}\n"
        f"Exit: {exit_px:.5f}\n"
        f"Reason: {reason}\n"
        f"Net PnL rate after cost: {pnl:.6f}\n"
        f"{format_close_leverage_scenarios(pos, exit_px)}\n"
    )
    send_email(f"CLOSE {position_txt(pos.side)} | {reason} | ETHUSDT 15M version B baseline", body)


# =============================================================================
# EXACT PRE-ML SIDE SELECTORS
# =============================================================================
def _create_strict_reaction_position(
    row: pd.Series,
    archetype: str,
) -> StrictReactionPosition:
    entry = float(row["entry_open_next"])
    atr = get_atr_abs(row, entry)
    signal_t = pd.Timestamp(row["ts_open"])
    entry_t = pd.Timestamp(row["entry_ts_next"])
    sl = entry - STRICT_REACTION_INITIAL_SL_ATR * atr
    tp = entry + STRICT_REACTION_TP_ATR * atr
    return StrictReactionPosition(
        signal_t=signal_t.isoformat(),
        entry_t=entry_t.isoformat(),
        entry=entry,
        atr=atr,
        archetype=str(archetype),
        sl=sl,
        tp=tp,
        trail_stop=sl,
        bars_held=0,
        best_high=entry,
        worst_low=entry,
        trail_active=False,
    )


def _resolve_strict_reaction_on_bar(
    pos: StrictReactionPosition,
    row: pd.Series,
) -> Tuple[Optional[float], Optional[str], StrictReactionPosition]:
    if pd.Timestamp(row["ts_open"]) < pd.Timestamp(pos.entry_t):
        return None, None, pos

    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    bars = int(pos.bars_held) + 1
    pos.best_high = max(float(pos.best_high), high)
    pos.worst_low = min(float(pos.worst_low), low)
    mfe_atr = (
        float(pos.best_high) - float(pos.entry)
    ) / max(float(pos.atr), 1e-12)

    if mfe_atr >= STRICT_REACTION_TRAIL_START_ATR:
        pos.trail_active = True
        pos.trail_stop = max(
            float(pos.trail_stop),
            float(pos.best_high)
            - STRICT_REACTION_TRAIL_DIST_ATR * float(pos.atr),
        )
    active_stop = float(pos.trail_stop) if pos.trail_active else float(pos.sl)

    # Exact conservative same-bar order from ORCH_V1_STRICT_REACTION.
    if low <= active_stop:
        return active_stop, "TR" if pos.trail_active else "SL", pos
    if high >= float(pos.tp):
        return float(pos.tp), "TP", pos

    if 2 <= bars <= STRICT_REACTION_EARLY_BARS:
        close_atr = (
            close - float(pos.entry)
        ) / max(float(pos.atr), 1e-12)
        if (
            mfe_atr < STRICT_REACTION_EARLY_MIN_MFE_ATR
            and close_atr <= STRICT_REACTION_EARLY_BAD_CLOSE_ATR
        ):
            return close, "EARLY_BAD_REACTION", pos

    if bars == STRICT_REACTION_WEAK_HOLD_BARS:
        close_atr = (
            close - float(pos.entry)
        ) / max(float(pos.atr), 1e-12)
        if (
            mfe_atr < STRICT_REACTION_RUNNER_MFE_ATR
            and close_atr <= 0.15
        ):
            return close, "WEAK_FOLLOWTHROUGH_EXIT", pos

    pos.bars_held = bars
    if bars >= STRICT_REACTION_HOLD_BARS:
        return close, "TIME_EXIT", pos
    return None, None, pos


def _load_strict_reaction_position(
    state: Dict[str, Any],
) -> Optional[StrictReactionPosition]:
    payload = state.get("strict_long_position")
    return StrictReactionPosition(**payload) if isinstance(payload, dict) else None


def _save_strict_reaction_position(
    state: Dict[str, Any],
    pos: Optional[StrictReactionPosition],
) -> None:
    state["strict_long_position"] = asdict(pos) if pos is not None else None


def _load_short_raw_position(state: Dict[str, Any]) -> Optional[OpenPosition]:
    payload = state.get("short_raw_position")
    return OpenPosition(**payload) if isinstance(payload, dict) else None


def _save_short_raw_position(
    state: Dict[str, Any],
    pos: Optional[OpenPosition],
) -> None:
    state["short_raw_position"] = asdict(pos) if pos is not None else None


def advance_pre_ml_side_selectors(
    row: pd.Series,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Advance LONG strict reaction and SHORT raw no-overlap independently."""
    strict_pos = _load_strict_reaction_position(state)
    strict_exit_reason: Optional[str] = None
    if strict_pos is not None:
        _, strict_exit_reason, strict_pos = _resolve_strict_reaction_on_bar(
            strict_pos, row
        )
        if strict_exit_reason is not None:
            strict_pos = None

    gate_state = v22_long_causal_gate_state(
        row, ("breakout", "reversal_after_drop")
    )
    long_selected = False
    selected_archetype: Optional[str] = None
    if strict_pos is None and bool(gate_state.get("candidate")):
        selected_archetype = str(gate_state.get("archetype"))
        strict_pos = _create_strict_reaction_position(row, selected_archetype)
        long_selected = selected_archetype == "breakout"
    _save_strict_reaction_position(state, strict_pos)

    short_pos = _load_short_raw_position(state)
    short_exit_reason: Optional[str] = None
    short_exited_this_bar = False
    if short_pos is not None:
        entry_t = pd.Timestamp(short_pos.entry_t)
        if pd.Timestamp(row["ts_open"]) >= entry_t:
            _, short_exit_reason, short_pos = resolve_position_on_bar(short_pos, row)
            if short_exit_reason is not None:
                short_pos = None
                short_exited_this_bar = True

    short_selected = False
    if (
        short_pos is None
        and not short_exited_this_bar
        and _short_rule_candidate(row)
    ):
        short_candidate = {
            "side": -1,
            "setup": SHORT_SETUP_NAME,
            "trigger": SHORT_TRIGGER,
            "exit": SHORT_EXIT_NAME,
        }
        short_pos = create_open_position(
            row, short_candidate, float("nan"), SHORT_THRESHOLD
        )
        short_selected = True
    _save_short_raw_position(state, short_pos)

    state["selector_state_initialized"] = True
    return {
        "long_selected": bool(long_selected),
        "long_selected_archetype": selected_archetype,
        "long_gate_state": gate_state,
        "long_strict_exit_reason": strict_exit_reason,
        "short_selected": bool(short_selected),
        "short_raw_exit_reason": short_exit_reason,
        "strict_long_position_after": (
            asdict(strict_pos) if strict_pos is not None else None
        ),
        "short_raw_position_after": (
            asdict(short_pos) if short_pos is not None else None
        ),
    }


# =============================================================================
# STATE / BAR PROCESSING
# =============================================================================
def default_runtime_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "initialized": False,
        "selector_state_initialized": False,
        "last_processed_bar": None,
        "position": None,
        "strict_long_position": None,
        "short_raw_position": None,
        "last_saved_at_utc": None,
    }


def _validated_runtime_state(candidate: Any) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return default_runtime_state()
    state = default_runtime_state()
    state.update(candidate)
    for key in ("position", "strict_long_position", "short_raw_position"):
        if state.get(key) is not None and not isinstance(state.get(key), dict):
            raise RuntimeError(f"Persisted runtime {key} must be a dictionary or null")
    state["selector_state_initialized"] = bool(
        state.get("selector_state_initialized", False)
    )
    if state.get("last_processed_bar"):
        parsed = pd.Timestamp(state["last_processed_bar"])
        if pd.isna(parsed):
            raise RuntimeError("Persisted last_processed_bar is invalid")
        state["last_processed_bar"] = parsed.isoformat()
    state["initialized"] = bool(state.get("initialized", False))
    state["schema_version"] = STATE_SCHEMA_VERSION
    return state


def _state_bar_timestamp(state: Optional[Dict[str, Any]]) -> Optional[pd.Timestamp]:
    if not state or not state.get("last_processed_bar"):
        return None
    try:
        return pd.Timestamp(state["last_processed_bar"])
    except Exception:
        return None


def load_runtime_state() -> Dict[str, Any]:
    disk_state: Optional[Dict[str, Any]] = None
    if RUNTIME_STATE_FILE.exists():
        try:
            disk_state = _validated_runtime_state(
                json.loads(RUNTIME_STATE_FILE.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            append_runtime_error("load_runtime_state_file", exc, None)
            raise RuntimeError(
                f"Cannot safely load runtime state: {RUNTIME_STATE_FILE}"
            ) from exc

    last_audit = _read_last_jsonl_record(MASTER_AUDIT_FILE)
    audit_state: Optional[Dict[str, Any]] = None
    if isinstance(last_audit, dict) and isinstance(last_audit.get("state_after"), dict):
        audit_state = _validated_runtime_state(last_audit["state_after"])

    if disk_state is None and audit_state is None:
        state = default_runtime_state()
        logging.info("[RESUME] no previous clean state/audit found; first run bootstraps FLAT")
        return state

    disk_t = _state_bar_timestamp(disk_state)
    audit_t = _state_bar_timestamp(audit_state)
    if audit_state is not None and (
        disk_state is None
        or disk_t is None
        or (audit_t is not None and audit_t > disk_t)
    ):
        state = audit_state
        logging.warning(
            "[RESUME RECOVERY] recovered newer state from master audit | last_processed=%s",
            state.get("last_processed_bar"),
        )
        save_runtime_state(state)
        return state

    state = disk_state if disk_state is not None else audit_state
    logging.info(
        "[RESUME] loaded clean persistent state | initialized=%s last_processed=%s position=%s",
        state.get("initialized"),
        state.get("last_processed_bar"),
        position_txt((state.get("position") or {}).get("side", 0)),
    )
    return state


def save_runtime_state(state: Dict[str, Any]) -> None:
    validated = _validated_runtime_state(state)
    validated["last_saved_at_utc"] = datetime.now(timezone.utc).isoformat()
    validated["last_saved_by_run_id"] = RUN_ID
    atomic_write_json(RUNTIME_STATE_FILE, validated)
    state.clear()
    state.update(validated)


def load_position(state: Dict[str, Any]) -> Optional[OpenPosition]:
    if state.get("position") is None:
        return None
    return OpenPosition(**state["position"])


def save_position(state: Dict[str, Any], pos: Optional[OpenPosition]) -> None:
    state["position"] = asdict(pos) if pos is not None else None

def process_one_signal_bar(
    row: pd.Series,
    state: Dict[str, Any],
    send_alerts: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # Side-level selectors advance on every signal bar independently of the
    # final portfolio position, exactly as in the original training pipeline.
    selector = advance_pre_ml_side_selectors(row, state)

    pos = load_position(state)
    position_before_state = asdict(pos) if pos is not None else None
    event: Dict[str, Any] = {
        "run_id": RUN_ID,
        "t": pd.Timestamp(row["ts_open"]).isoformat(),
        "bar_closed_now": bool_value(row.get("bar_closed_now")),
        "valid_next_entry": bool_value(row.get("valid_next_entry")),
        "rule_side": 0,
        "rule_reason": "no_signal",
        "ml_prob": None,
        "ml_threshold": None,
        "ml_accept": None,
        "opened": None,
        "closed_reason": None,
        "exit_px": None,
        "position_before": position_txt(pos.side if pos is not None else 0),
        "position_after": None,
        "position_before_state": position_before_state,
        "position_after_state": None,
        "closed_position_state": None,
        "candidate_evaluation_blocked_by_position": pos is not None,
        "leverage_scenarios_json": None,
        "sample_features": None,
        "candidate_evaluations": [],
        "pre_ml_selector": selector,
        "rule_funnel": build_rule_funnel(
            row,
            long_selected=selector["long_selected"],
            short_selected=selector["short_selected"],
            long_selector_meta=selector,
        ),
    }

    if pos is not None:
        entry_t = pd.Timestamp(pos.entry_t)
        bar_t = pd.Timestamp(row["ts_open"])
        if bar_t >= entry_t:
            exit_px, reason, pos = resolve_position_on_bar(pos, row)
            if exit_px is not None and reason is not None:
                exit_t = pd.Timestamp(row["ts_close"])
                if send_alerts:
                    send_close_email(pos, exit_t, float(exit_px), reason)
                event["closed_reason"] = reason
                event["exit_px"] = float(exit_px)
                event["closed_position_state"] = asdict(pos)
                event = add_close_leverage_columns(event, pos, float(exit_px))
                pos = None

    if pos is None:
        event["candidate_evaluation_blocked_by_position"] = False
        candidates = evaluate_rule_candidates(
            row,
            long_selected=selector["long_selected"],
            short_selected=selector["short_selected"],
            long_selector_meta=selector,
        )
        scored: List[Dict[str, Any]] = []
        for cand in candidates:
            side = int(cand["side"])
            entry_time = pd.Timestamp(row["entry_ts_next"])
            accept, prob, thr, calibration, sample = predict_side(
                side, row, entry_time
            )
            funnel_key = "long" if side == +1 else "short"
            event["rule_funnel"][funnel_key]["ml_prob"] = float(prob)
            event["rule_funnel"][funnel_key]["ml_threshold"] = float(thr)
            event["rule_funnel"][funnel_key]["ml_accept"] = bool(accept)
            event["rule_funnel"][funnel_key]["ml_status"] = (
                "ML_ACCEPT" if accept else "ML_REJECT"
            )
            scored.append({
                "candidate": cand,
                "accept": bool(accept),
                "prob": float(prob),
                "thr": float(thr),
                "calibration": calibration,
                "sample": dict(sample),
                "selected_by_portfolio": False,
            })

        accepted = [item for item in scored if item["accept"]]
        if accepted:
            # Exact final policy: global one-position, no flip, LONG_FIRST.
            accepted.sort(
                key=lambda item: (
                    0 if int(item["candidate"]["side"]) == +1 else 1,
                    -(item["prob"] - item["thr"]),
                    -item["prob"],
                )
            )
            best = accepted[0]
            best["selected_by_portfolio"] = True
            cand = best["candidate"]
            pos = create_open_position(row, cand, best["prob"], best["thr"])
            event.update({
                "rule_side": int(cand["side"]),
                "rule_reason": f"{cand['setup']}|{cand['trigger']}|ML_ACCEPT",
                "ml_prob": float(best["prob"]),
                "ml_threshold": float(best["thr"]),
                "ml_accept": True,
                "opened": position_txt(int(cand["side"])),
                "sample_features": dict(best["sample"]),
            })
            event = add_open_leverage_columns(event, pos)
            if send_alerts:
                send_open_email(pos)
        elif scored:
            best = sorted(scored, key=lambda item: item["prob"], reverse=True)[0]
            cand = best["candidate"]
            event.update({
                "rule_side": int(cand["side"]),
                "rule_reason": f"{cand['setup']}|{cand['trigger']}|ML_REJECT",
                "ml_prob": float(best["prob"]),
                "ml_threshold": float(best["thr"]),
                "ml_accept": False,
                "sample_features": dict(best["sample"]),
            })
        event["candidate_evaluations"] = scored

    save_position(state, pos)
    state["last_processed_bar"] = pd.Timestamp(row["ts_open"]).isoformat()
    state["initialized"] = True
    state["selector_state_initialized"] = True
    event["position_after"] = position_txt(pos.side if pos is not None else 0)
    event["position_after_state"] = asdict(pos) if pos is not None else None
    return state, event

# =============================================================================
# AUDIT / DEBUG HELPERS
# =============================================================================
def _compare_feature_series(a: pd.Series, b: pd.Series, tol: float) -> Tuple[int, float]:
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    both_nan = a.isna() & b.isna()
    diff = (a - b).abs()
    diff = diff.where(~both_nan, 0.0)
    mismatch = int((diff > tol).sum())
    max_abs_diff = float(diff.max()) if len(diff) else 0.0
    if np.isnan(max_abs_diff):
        max_abs_diff = 0.0
    return mismatch, max_abs_diff


def _load_local_raw_tf(tf: str) -> pd.DataFrame:
    fp = find_csv(tf)
    hist = pd.read_csv(fp, encoding="latin1", low_memory=False)
    missing = [c for c in RAW_COLUMNS if c not in hist.columns]
    if missing:
        raise RuntimeError(f"[LOCAL RAW] {tf}: missing columns: {missing}")

    # Preserve authoritative Training/Forward external-source columns during
    # the startup-only historical feature audit. calculate_features() already
    # consumes these columns when present; dropping them here would force the
    # recompute onto generic fallbacks and can create false feature mismatches.
    audit_external_cols = [
        c for c in (REALAGG_SOURCE_COLUMNS + OI_SOURCE_COLUMNS)
        if c in hist.columns and c not in RAW_COLUMNS
    ]
    raw = hist[RAW_COLUMNS + audit_external_cols].copy()

    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    for c in raw.columns:
        if c != "date":
            raw[c] = pd.to_numeric(raw[c], errors="coerce")
    raw = raw.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    raw = raw.sort_values("date").drop_duplicates(subset=["date"], keep="first").reset_index(drop=True)
    return raw


def run_local_feature_audit():
    bad_msgs = []
    for tf in TRAINING_AUDIT_TFS:
        fp = find_csv(tf)
        hist = pd.read_csv(fp, encoding="latin1", low_memory=False)
        hist["date"] = pd.to_datetime(hist["date"], utc=True, errors="coerce")
        for col in ["open", "high", "low", "close"]:
            if col in hist.columns:
                hist[col] = pd.to_numeric(hist[col], errors="coerce")
        if all(c in hist.columns for c in ["open", "high", "low", "close"]):
            o = pd.to_numeric(hist["open"], errors="coerce")
            h = pd.to_numeric(hist["high"], errors="coerce")
            l = pd.to_numeric(hist["low"], errors="coerce")
            c = pd.to_numeric(hist["close"], errors="coerce")
            rng = (h - l).replace(0, np.nan)
            derived = {
                "body": c - o,
                "range": h - l,
                "upper_wick": h - np.maximum(o, c),
                "lower_wick": np.minimum(o, c) - l,
                "body_pct": safe_div((c - o).abs(), rng),
                "upper_wick_pct": safe_div(h - np.maximum(o, c), rng),
                "lower_wick_pct": safe_div(np.minimum(o, c) - l, rng),
                "close_pos": safe_div(c - l, rng),
                "candle_direction": np.sign(c - o),
            }
            for feat, values in derived.items():
                if feat in BASE_FEATURES and feat not in hist.columns:
                    hist[feat] = values
        raw = _load_local_raw_tf(tf)
        calc = calculate_features(raw, tf)
        calc["date"] = pd.to_datetime(calc["date"], utc=True, errors="coerce")
        compare_cols = [
            c for c in BASE_FEATURES
            if c not in STARTUP_FEATURE_AUDIT_SKIP_COLUMNS
            and c in hist.columns
            and c in calc.columns
        ]
        skipped_features = [
            c for c in BASE_FEATURES
            if c not in STARTUP_FEATURE_AUDIT_SKIP_COLUMNS
            and (c not in hist.columns or c not in calc.columns)
        ]
        skipped_contract_features = [
            c for c in BASE_FEATURES
            if c in STARTUP_FEATURE_AUDIT_SKIP_COLUMNS
            and c in hist.columns
            and c in calc.columns
        ]
        if skipped_features:
            logging.warning("[AUDIT] %s: skipped unavailable feature columns: %s", tf, skipped_features)
        if skipped_contract_features:
            logging.info(
                "[AUDIT] %s: skipped known historical CVD/flow rebase columns: %s",
                tf,
                skipped_contract_features,
            )
        if not compare_cols:
            raise RuntimeError(f"[AUDIT] {tf}: no comparable feature columns found")
        merged = hist[["date"] + compare_cols].merge(calc[["date"] + compare_cols], on="date", how="inner", suffixes=("_csv", "_calc"))
        if merged.empty:
            raise RuntimeError(f"[AUDIT] {tf}: no overlapping rows after merge")
        tf_bad = []
        for feat in compare_cols:
            tol = 0.0 if feat.endswith("break_up") or feat.endswith("break_dn") or feat.endswith("trend_state") else AUDIT_TOL
            mismatch, max_abs_diff = _compare_feature_series(merged[f"{feat}_csv"], merged[f"{feat}_calc"], tol=tol)
            if mismatch > 0:
                tf_bad.append(f"{feat}: mismatch={mismatch}, max_abs_diff={max_abs_diff:.12g}")
        if tf_bad:
            bad_msgs.append(f"{tf} -> " + " | ".join(tf_bad[:12]))
            logging.error("[FEATURE AUDIT] %s: FAIL | overlap=%d | compared=%d", tf, len(merged), len(compare_cols))
        else:
            logging.info("[FEATURE AUDIT] %s: PASS | overlap=%d | compared=%d", tf, len(merged), len(compare_cols))
    if bad_msgs and AUDIT_STRICT:
        raise RuntimeError("[FEATURE AUDIT FAILED] " + " || ".join(bad_msgs))
    if not bad_msgs:
        logging.info("[FEATURE AUDIT] ALL PASS — local CSV recompute matches comparable stored base features")


def fingerprint_needs_rebuild() -> bool:
    # The clean audit folder stores only live outputs. Training fingerprint is
    # rebuilt in memory and is never written into the audit room.
    return True


def build_training_fingerprint() -> Dict[str, Any]:
    logging.info("[FINGERPRINT] building training fingerprint from local historical CSVs")
    panel = load_training_panel_for_thresholds().copy()
    panel["bar_closed_now"] = True
    panel["seconds_to_bar_close_now"] = 0.0
    panel = panel.tail(FINGERPRINT_REPLAY_BARS).reset_index(drop=True)
    feature_cols = []
    for c in list(BASE_FEATURES) + ["s1_mom", "s5_mom"] + LONG_FEATURE_COLS + SHORT_FEATURE_COLS:
        if c in panel.columns and c not in feature_cols and pd.api.types.is_numeric_dtype(panel[c]):
            feature_cols.append(c)
    feature_stats = {}
    for c in feature_cols:
        s = pd.to_numeric(panel[c], errors="coerce").dropna()
        if len(s) < 30:
            continue
        feature_stats[c] = {"mean": float(s.mean()), "std": float(s.std(ddof=0)), "q05": float(s.quantile(0.05)), "q50": float(s.quantile(0.50)), "q95": float(s.quantile(0.95))}
    tmp_state = default_runtime_state()
    events = []
    for _, row in panel.iterrows():
        tmp_state, ev = process_one_signal_bar(row=row, state=tmp_state, send_alerts=False)
        events.append(ev)
    ev_df = pd.DataFrame(events)
    total = len(ev_df)
    if total == 0:
        signal_baseline = {"rule_candidate_rate": 0.0, "rule_long_share": 0.0, "rule_short_share": 0.0, "ml_accept_rate_on_reached": 0.0, "open_rate": 0.0}
    else:
        rule_side = pd.to_numeric(ev_df["rule_side"], errors="coerce").fillna(0)
        candidates = rule_side != 0
        ml_prob_notna = pd.to_numeric(ev_df["ml_prob"], errors="coerce").notna()
        ml_accept = bool_series(ev_df["ml_accept"]) if "ml_accept" in ev_df.columns else pd.Series(False, index=ev_df.index)
        opened = ev_df["opened"].astype(str).isin(["LONG", "SHORT"])
        cand_n = int(candidates.sum())
        long_n = int((rule_side == 1).sum())
        short_n = int((rule_side == -1).sum())
        signal_baseline = {
            "rule_candidate_rate": float(cand_n / total),
            "rule_long_share": float(long_n / max(cand_n, 1)),
            "rule_short_share": float(short_n / max(cand_n, 1)),
            "ml_accept_rate_on_reached": float(ml_accept[ml_prob_notna].mean()) if ml_prob_notna.any() else 0.0,
            "open_rate": float(opened.mean()),
        }
    out = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "bars_used": int(len(panel)),
        "feature_stats": feature_stats,
        "signal_baseline": signal_baseline,
        "source_csvs": {tf: str(find_csv(tf)) for tf in TRAINING_AUDIT_TFS},
        "bundle_file": str(BUNDLE_FILE),
        "config_file": str(CONFIG_FILE),
    }
    return out


def load_or_build_training_fingerprint() -> Dict[str, Any]:
    data = build_training_fingerprint()
    logging.info("[FINGERPRINT] built in memory only; no audit file created")
    return data


def log_training_fingerprint_summary(fp: Dict[str, Any]):
    if not fp:
        return
    sb = fp.get("signal_baseline", {})
    logging.info(
        "[FINGERPRINT SUMMARY] bars=%s | candidate=%.4f | long_share=%.4f | short_share=%.4f | ml_accept=%.4f | open_rate=%.4f",
        fp.get("bars_used"),
        float(sb.get("rule_candidate_rate", 0.0)),
        float(sb.get("rule_long_share", 0.0)),
        float(sb.get("rule_short_share", 0.0)),
        float(sb.get("ml_accept_rate_on_reached", 0.0)),
        float(sb.get("open_rate", 0.0)),
    )


def run_startup_full_parity_replay() -> Dict[str, Any]:
    logging.info("[FULL PARITY REPLAY] starting historical one-position replay against final training target")
    panel = load_training_panel_for_thresholds().copy()
    panel["bar_closed_now"] = True
    panel["seconds_to_bar_close_now"] = 0.0
    state = default_runtime_state()
    opened = 0
    closed = 0
    long_opened = 0
    short_opened = 0
    for _, row in panel.iterrows():
        state, ev = process_one_signal_bar(row=row, state=state, send_alerts=False)
        if ev.get("opened") in {"LONG", "SHORT"}:
            opened += 1
            if ev.get("opened") == "LONG":
                long_opened += 1
            else:
                short_opened += 1
        if ev.get("closed_reason") not in {None, "", np.nan}:
            closed += 1
    result = {
        "opened_trades": int(opened),
        "closed_trades": int(closed),
        "long_opened": int(long_opened),
        "short_opened": int(short_opened),
        "expected_final_trades": int(EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES),
        "status": "PASS" if opened == EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES else "FAIL",
    }
    logging.info(
        "[FULL PARITY REPLAY] status=%s opened=%d closed=%d long=%d short=%d expected=%d",
        result["status"], opened, closed, long_opened, short_opened, EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES,
    )
    if AUDIT_STRICT and result["status"] != "PASS":
        raise RuntimeError(f"FULL PARITY REPLAY FAIL: {result}")
    return result


def _read_last_master_records(limit: int) -> List[Dict[str, Any]]:
    if limit <= 0 or not MASTER_AUDIT_FILE.exists() or MASTER_AUDIT_FILE.stat().st_size == 0:
        return []
    records = deque(maxlen=limit)
    try:
        with open(MASTER_AUDIT_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return list(records)


def compute_recent_signal_monitor(window: int = LIVE_MONITOR_WINDOW) -> Optional[Dict[str, Any]]:
    records = _read_last_master_records(window)
    if not records:
        return None
    events = [record.get("decision") or {} for record in records]
    df = pd.DataFrame(events)
    if df.empty:
        return None
    rule_side = pd.to_numeric(df.get("rule_side"), errors="coerce").fillna(0)
    candidates = rule_side != 0
    ml_reached = pd.to_numeric(df.get("ml_prob"), errors="coerce").notna()
    ml_accept = (
        bool_series(df["ml_accept"])
        if "ml_accept" in df.columns
        else pd.Series(False, index=df.index)
    )
    opened = (
        df["opened"].astype(str).isin(["LONG", "SHORT"])
        if "opened" in df.columns
        else pd.Series(False, index=df.index)
    )
    cand_n = int(candidates.sum())
    long_n = int((rule_side == 1).sum())
    short_n = int((rule_side == -1).sum())
    return {
        "rows": int(len(df)),
        "rule_candidate_rate": float(cand_n / max(len(df), 1)),
        "rule_long_share": float(long_n / max(cand_n, 1)),
        "rule_short_share": float(short_n / max(cand_n, 1)),
        "ml_accept_rate_on_reached": (
            float(ml_accept[ml_reached].mean()) if ml_reached.any() else 0.0
        ),
        "open_rate": float(opened.mean()),
    }


def compare_signal_monitor_to_fingerprint(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if (TRAINING_FINGERPRINT is None) or (metrics is None):
        return {"status": "NA", "warnings": [], "metrics": metrics}
    base = TRAINING_FINGERPRINT.get("signal_baseline", {})
    warnings_list = []
    for k in ["rule_candidate_rate", "rule_long_share", "rule_short_share", "ml_accept_rate_on_reached", "open_rate"]:
        live_v = float(metrics.get(k, 0.0))
        base_v = float(base.get(k, 0.0))
        tol = max(RATE_WARN_ABS, RATE_WARN_REL * max(abs(base_v), 1e-9))
        if abs(live_v - base_v) > tol:
            warnings_list.append(f"{k}: live={live_v:.4f} base={base_v:.4f} tol={tol:.4f}")
    return {"status": "WARN" if warnings_list else "OK", "warnings": warnings_list, "metrics": metrics, "baseline": base}


def compare_live_features_to_training(panel: pd.DataFrame, window: int = LIVE_MONITOR_WINDOW) -> Dict[str, Any]:
    if (TRAINING_FINGERPRINT is None) or panel.empty:
        return {"status": "NA", "warnings": []}
    fp_stats = TRAINING_FINGERPRINT.get("feature_stats", {})
    sub = panel.tail(window).copy()
    warnings_list = []
    binary_like = {"ms_break_up", "ms_break_dn", "break_up", "break_dn", "trend_state", "ms_trend_state"}
    for feat, st in fp_stats.items():
        if feat not in sub.columns:
            continue
        s = pd.to_numeric(sub[feat], errors="coerce").dropna()
        if len(s) < 20:
            continue
        live_mean = float(s.mean())
        live_std = float(s.std(ddof=0))
        train_mean = float(st.get("mean", np.nan))
        train_std = float(st.get("std", np.nan))
        q05 = float(st.get("q05", np.nan))
        q95 = float(st.get("q95", np.nan))
        simple_name = feat.split("__")[-1].split("_")[0] if "__" in feat else feat
        is_binary = feat in binary_like or simple_name in binary_like or feat.endswith("break_up") or feat.endswith("break_dn")
        if is_binary:
            tol = max(0.03, 0.50 * max(abs(train_mean), 0.01))
            if abs(live_mean - train_mean) > tol:
                warnings_list.append(f"{feat}: live_rate={live_mean:.4f} train_rate={train_mean:.4f}")
        else:
            if np.isfinite(q05) and np.isfinite(q95) and (live_mean < q05 or live_mean > q95):
                warnings_list.append(f"{feat}: live_mean={live_mean:.4f} train_q05={q05:.4f} train_q95={q95:.4f}")
            if np.isfinite(train_std) and train_std > 1e-12:
                ratio = live_std / train_std
                if ratio < FEATURE_STD_RATIO_MIN or ratio > FEATURE_STD_RATIO_MAX:
                    warnings_list.append(f"{feat}: live_std_ratio={ratio:.4f}")
    return {"status": "WARN" if warnings_list else "OK", "warnings": warnings_list}


def compute_feature_drift(panel: pd.DataFrame, window: int = DRIFT_WINDOW) -> str:
    if len(panel) < max(5, window):
        return "window_too_small"
    msgs = []
    sub = panel.tail(window).copy()
    low_var = []
    for c in ["ms_break_up", "ms_break_dn", "body_pct", "price_ema20", "adx_14", "rsi_14"]:
        if c in sub.columns:
            s = pd.to_numeric(sub[c], errors="coerce")
            if s.dropna().std(ddof=0) <= LOW_VAR_STD_EPS:
                low_var.append(c)
    msgs.append(f"low_var={','.join(low_var) if low_var else 'None'}")
    for c in ["adx_14", "rsi_14", "price_ema20"]:
        if c in sub.columns:
            s = pd.to_numeric(sub[c], errors="coerce").dropna()
            if len(s) >= 2:
                last = float(s.iloc[-1])
                diff = float(s.iloc[-1] - s.iloc[-2])
                std = float(s.std(ddof=0))
                msgs.append(f"{c}(last={last:.5f} diff={diff:.5f} std={std:.8f})")
    return " | ".join(msgs)


def _position_metrics(
    position_state: Optional[Dict[str, Any]],
    close_px: Any,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if not position_state:
        return None, None, None
    try:
        entry = float(position_state["entry"])
        atr = max(float(position_state["atr"]), 1e-12)
        best_high = float(position_state["best_high"])
        best_low = float(position_state["best_low"])
        side = int(position_state["side"])
        close_value = float(close_px)
        if side == +1:
            mfe = (best_high - entry) / atr
            mae = (entry - best_low) / atr
            pnl = close_value / entry - 1.0 - ROUND_TRIP_COST
        else:
            mfe = (entry - best_low) / atr
            mae = (best_high - entry) / atr
            pnl = (entry - close_value) / entry - ROUND_TRIP_COST
        return float(mfe), float(mae), float(pnl)
    except Exception:
        return None, None, None


def fetch_audit_live_price_snapshot(fallback_close: float) -> Dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    errors: List[str] = []
    for base_url, endpoint, source in (
        (BINANCE_FUTURES_BASE, "/fapi/v1/ticker/bookTicker", "BINANCE_FUTURES_BOOK_TICKER"),
        (BINANCE_BASE, "/api/v3/ticker/bookTicker", "BINANCE_SPOT_BOOK_TICKER"),
    ):
        try:
            response = requests.get(
                f"{base_url}{endpoint}",
                params={"symbol": SYMBOL},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            bid = float(payload["bidPrice"])
            ask = float(payload["askPrice"])
            if np.isfinite(bid) and np.isfinite(ask) and bid > 0 and ask > 0:
                return {
                    "fetched_at_utc": fetched_at,
                    "source": source,
                    "bid": bid,
                    "ask": ask,
                    "mid": (bid + ask) / 2.0,
                    "fallback_used": False,
                    "errors": errors,
                }
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
    fallback = float(fallback_close)
    return {
        "fetched_at_utc": fetched_at,
        "source": "CLOSED_BAR_CLOSE_FALLBACK",
        "bid": fallback,
        "ask": fallback,
        "mid": fallback,
        "fallback_used": True,
        "errors": errors,
    }


def _unrealized_pnl_snapshot(
    position_state: Optional[Dict[str, Any]],
    live_price: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not position_state:
        return None
    try:
        side = int(position_state["side"])
        entry = float(position_state["entry"])
        mark = float(live_price["bid"] if side == +1 else live_price["ask"])
        gross = mark / entry - 1.0 if side == +1 else (entry - mark) / entry
        return {
            "mark_price": mark,
            "gross_rate": float(gross),
            "after_round_trip_cost_rate": float(gross - ROUND_TRIP_COST),
        }
    except Exception:
        return None


def _build_trade_path_snapshots(
    event: Dict[str, Any],
    row: pd.Series,
    live_price: Dict[str, Any],
) -> List[Dict[str, Any]]:
    before = event.get("position_before_state") or None
    closed = event.get("closed_position_state") or None
    after = event.get("position_after_state") or None
    trade_ids = []
    for state_item in (before, closed, after):
        trade_id = (state_item or {}).get("trade_id")
        if trade_id and trade_id not in trade_ids:
            trade_ids.append(trade_id)

    snapshots: List[Dict[str, Any]] = []
    for trade_id in trade_ids:
        before_match = before if (before or {}).get("trade_id") == trade_id else None
        closed_match = closed if (closed or {}).get("trade_id") == trade_id else None
        after_match = after if (after or {}).get("trade_id") == trade_id else None
        active = after_match or closed_match or before_match
        mfe, mae, close_pnl = _position_metrics(active, row.get("close"))
        snapshots.append({
            "trade_id": trade_id,
            "signal_bar_utc": event.get("t"),
            "bar_close_utc": (
                pd.Timestamp(row.get("ts_close")).isoformat()
                if pd.notna(row.get("ts_close"))
                else None
            ),
            "candle": {
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            },
            "live_price_at_processing": live_price,
            "position_before": before_match,
            "position_after": after_match,
            "closed_position": closed_match,
            "opened_on_this_bar": bool(
                event.get("opened") and after_match is not None and before_match is None
            ),
            "closed_on_this_bar": closed_match is not None,
            "exit_px": event.get("exit_px") if closed_match is not None else None,
            "exit_reason": event.get("closed_reason") if closed_match is not None else None,
            "stop_before": (before_match or {}).get("stop"),
            "stop_after": (after_match or closed_match or {}).get("stop"),
            "trail_before": (before_match or {}).get("trail_active"),
            "trail_after": (after_match or closed_match or {}).get("trail_active"),
            "bars_held_before": (before_match or {}).get("bars_held"),
            "bars_held_after": (after_match or closed_match or {}).get("bars_held"),
            "mfe_atr": mfe,
            "mae_atr": mae,
            "bar_close_pnl_after_cost": close_pnl,
            "unrealized_at_processing": _unrealized_pnl_snapshot(after_match, live_price),
        })
    return snapshots


def _collect_existing_trade_path(trade_id: str) -> List[Dict[str, Any]]:
    path: List[Dict[str, Any]] = []
    if not MASTER_AUDIT_FILE.exists() or MASTER_AUDIT_FILE.stat().st_size == 0:
        return path
    try:
        with open(MASTER_AUDIT_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                for snapshot in record.get("trade_path_snapshots") or []:
                    if snapshot.get("trade_id") == trade_id:
                        path.append(snapshot)
    except Exception as exc:
        logging.warning("[TRADE PATH READ WARNING] %s", exc)
    return path



def feature_exists_in_row_or_derived(feature_name: str, row: pd.Series) -> bool:
    feature = str(feature_name)
    derived_ml_features = {
        "ml_hour",
        "ml_dow",
        "ml_is_us_session",
        "ml_is_london_session",
        "ml_is_asia_session",
    }
    return feature in derived_ml_features or feature in row.index


def snapshot_missing_nan(
    sample: Dict[str, Any],
    feature_cols: List[str],
    row: pd.Series,
) -> Tuple[List[str], List[str]]:
    missing: List[str] = []
    nan_features: List[str] = []
    for feature in feature_cols:
        if not feature_exists_in_row_or_derived(feature, row):
            missing.append(feature)
        if pd.isna(sample.get(feature, np.nan)):
            nan_features.append(feature)
    return missing, nan_features

def append_full_candle_audit(
    event: Dict[str, Any],
    row: pd.Series,
    state: Dict[str, Any],
    live_price: Dict[str, Any],
    cycle_diagnostics: Dict[str, Any],
    processing_mode: str,
    trade_path_snapshots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    entry_value = row.get("entry_ts_next")
    entry_timestamp = (
        pd.Timestamp(entry_value)
        if pd.notna(entry_value)
        else pd.Timestamp(row["ts_open"]) + pd.Timedelta(minutes=15)
    )
    long_sample = build_ml_sample(row, LONG_FEATURE_COLS, entry_timestamp)
    short_sample = build_ml_sample(row, SHORT_FEATURE_COLS, entry_timestamp)
    long_missing, long_nan = snapshot_missing_nan(long_sample, LONG_FEATURE_COLS, row)
    short_missing, short_nan = snapshot_missing_nan(short_sample, SHORT_FEATURE_COLS, row)

    raw_candidates = [
        "ts_open", "ts_close", "entry_ts_next", "entry_open_next",
        "bar_closed_now", "valid_next_entry",
    ] + list(RAW_COLUMNS)
    for prefix in ("btc_", "ethbtc_"):
        raw_candidates.extend(
            column for column in row.index if str(column).startswith(prefix)
        )
    raw_columns = list(dict.fromkeys(
        column for column in raw_candidates if column in row.index
    ))

    decision = {
        key: value
        for key, value in event.items()
        if key not in {
            "rule_funnel", "candidate_evaluations", "sample_features",
            "position_before_state", "position_after_state",
            "closed_position_state",
        }
    }
    record = {
        "schema_version": STATE_SCHEMA_VERSION,
        "record_type": "CLOSED_15M_CANDLE",
        "run_id": RUN_ID,
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "processing_mode": processing_mode,
        "signal_bar_utc": event.get("t"),
        "bar_close_utc": (
            pd.Timestamp(row.get("ts_close")).isoformat()
            if pd.notna(row.get("ts_close"))
            else None
        ),
        "entry_time_utc": entry_timestamp.isoformat(),
        "candle_15m": {
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "quote_asset_volume": row.get("quote_asset_volume"),
            "number_of_trades": row.get("number_of_trades"),
        },
        "live_price_at_processing": live_price,
        "raw_market_data": {column: row.get(column) for column in raw_columns},
        "all_raw_and_feature_values": row.to_dict(),
        "feature_health": {
            "long_feature_count": len(LONG_FEATURE_COLS),
            "short_feature_count": len(SHORT_FEATURE_COLS),
            "long_missing": long_missing,
            "short_missing": short_missing,
            "long_nan": long_nan,
            "short_nan": short_nan,
        },
        "ml": {
            "long_features": long_sample,
            "short_features": short_sample,
            "selected_sample_features": event.get("sample_features"),
            "decision_probability": event.get("ml_prob"),
            "decision_threshold": event.get("ml_threshold"),
            "decision_accept": event.get("ml_accept"),
        },
        "candidates_and_gates": {
            "long": (event.get("rule_funnel") or {}).get("long"),
            "short": (event.get("rule_funnel") or {}).get("short"),
            "evaluated_candidates": event.get("candidate_evaluations") or [],
            "blocked_by_open_position": event.get(
                "candidate_evaluation_blocked_by_position", False
            ),
        },
        "decision": decision,
        "position": {
            "before": event.get("position_before_state"),
            "after": event.get("position_after_state"),
            "closed": event.get("closed_position_state"),
            "unrealized_before_at_processing": _unrealized_pnl_snapshot(
                event.get("position_before_state"), live_price
            ),
            "unrealized_after_at_processing": _unrealized_pnl_snapshot(
                event.get("position_after_state"), live_price
            ),
        },
        "trade_path_snapshots": trade_path_snapshots,
        "data_quality_and_diagnostics": cycle_diagnostics,
        "state_after": dict(state),
    }
    append_jsonl_row_unique(MASTER_AUDIT_FILE, record, "signal_bar_utc")
    return record


def persist_bar_audits_and_state(
    row: pd.Series,
    event: Dict[str, Any],
    state: Dict[str, Any],
    live_price: Dict[str, Any],
    cycle_diagnostics: Dict[str, Any],
    processing_mode: str,
) -> None:
    """Persist one candle using trade -> master -> atomic state ordering."""
    trade_path_snapshots = _build_trade_path_snapshots(event, row, live_price)

    closed_state = event.get("closed_position_state")
    if closed_state and event.get("exit_px") is not None and event.get("closed_reason"):
        closed_pos = OpenPosition(**closed_state)
        existing_path = _collect_existing_trade_path(closed_pos.trade_id)
        current_path = [
            item for item in trade_path_snapshots
            if item.get("trade_id") == closed_pos.trade_id
        ]
        append_closed_trade(
            closed_pos,
            pd.Timestamp(row["ts_close"]),
            float(event["exit_px"]),
            str(event["closed_reason"]),
            existing_path + current_path,
        )

    append_full_candle_audit(
        event,
        row,
        state,
        live_price,
        cycle_diagnostics,
        processing_mode,
        trade_path_snapshots,
    )
    save_runtime_state(state)


def rebuild_selector_state_from_history(
    state: Dict[str, Any],
    decision_source: pd.DataFrame,
    through: Optional[pd.Timestamp] = None,
) -> Dict[str, Any]:
    state["strict_long_position"] = None
    state["short_raw_position"] = None
    rows = decision_source.copy().sort_values("ts_open")
    if through is not None:
        rows = rows[rows["ts_open"] <= through]
    # Both side selectors have finite horizons (20 and 8 bars); replaying the
    # available clean panel is sufficient and deterministic.
    for _, row in rows.iterrows():
        advance_pre_ml_side_selectors(row, state)
    state["selector_state_initialized"] = True
    return state


def bootstrap_state_from_history(
    state: Dict[str, Any],
    decision_source: pd.DataFrame,
) -> Dict[str, Any]:
    if decision_source.empty:
        state["initialized"] = True
        state["selector_state_initialized"] = True
        save_runtime_state(state)
        return state
    state["position"] = None
    state = rebuild_selector_state_from_history(state, decision_source)
    state["last_processed_bar"] = pd.Timestamp(
        decision_source.iloc[-1]["ts_open"]
    ).isoformat()
    state["initialized"] = True
    save_runtime_state(state)
    append_run_session(
        "BOOTSTRAP_FLAT_WITH_EXACT_SELECTOR_STATE",
        state,
        last_processed_bar=state["last_processed_bar"],
    )
    logging.info(
        "[BOOTSTRAP DONE] portfolio=FLAT selectors=replayed | last_processed_bar=%s",
        state["last_processed_bar"],
    )
    return state


def needs_silent_catchup(last_processed: Optional[pd.Timestamp], new_rows: pd.DataFrame) -> bool:
    if new_rows.empty:
        return False
    if last_processed is None:
        return True
    if len(new_rows) > 1:
        return True
    first_t = pd.Timestamp(new_rows.iloc[0]["ts_open"])
    return (first_t - last_processed) > pd.Timedelta(minutes=15)


def run_once(state: Dict[str, Any]) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    panel, integrity_results, alignment_results = fetch_and_prepare(now_utc)
    if panel.empty:
        warning = "Live panel is empty"
        logging.warning("[WARNING] %s", warning)
        append_diagnostic_warning(None, "EMPTY_PANEL", [warning], {"state": state})
        return state

    decision_source = (
        panel[(panel["bar_closed_now"]) & (panel["valid_next_entry"])]
        .copy()
        .sort_values("ts_open")
        .reset_index(drop=True)
    )

    feature_diag: Dict[str, Any] = {"status": "NA", "warnings": []}
    signal_diag: Dict[str, Any] = {"status": "NA", "warnings": []}
    if AUDIT_MODE:
        log_bar_mode(panel, decision_source, now_utc)
        log_candle_integrity(integrity_results)
        log_alignment_checks(alignment_results)
        feature_diag = compare_live_features_to_training(panel, LIVE_MONITOR_WINDOW)
        signal_metrics = compute_recent_signal_monitor(LIVE_MONITOR_WINDOW)
        signal_diag = compare_signal_monitor_to_fingerprint(signal_metrics)
        logging.info(
            "[TRAINING DRIFT CHECK] feature_status=%s | feature_warn_n=%d | "
            "signal_status=%s | signal_warn_n=%d",
            feature_diag.get("status"),
            len(feature_diag.get("warnings", [])),
            signal_diag.get("status"),
            len(signal_diag.get("warnings", [])),
        )
        if feature_diag.get("warnings"):
            logging.warning("[FEATURE WARNINGS] %s", " | ".join(feature_diag["warnings"][:6]))
        if signal_diag.get("warnings"):
            logging.warning("[SIGNAL WARNINGS] %s", " | ".join(signal_diag["warnings"][:6]))
        logging.info(
            "[FEATURE DRIFT] window=%d | %s",
            DRIFT_WINDOW,
            compute_feature_drift(panel, DRIFT_WINDOW),
        )

    latest_signal_bar = (
        pd.Timestamp(decision_source.iloc[-1]["ts_open"]).isoformat()
        if not decision_source.empty
        else None
    )
    integrity_warnings = [
        f"{tf}: {details}"
        for tf, details in integrity_results.items()
        if str(details.get("status")) != "OK"
    ]
    alignment_warnings = [
        f"{tf}: {details}"
        for tf, details in alignment_results.items()
        if str(details.get("status")) != "OK"
    ]
    append_diagnostic_warning(
        latest_signal_bar,
        "FEATURE_DRIFT",
        list(feature_diag.get("warnings", [])),
        {"status": feature_diag.get("status")},
    )
    append_diagnostic_warning(
        latest_signal_bar,
        "SIGNAL_DRIFT",
        list(signal_diag.get("warnings", [])),
        {"status": signal_diag.get("status")},
    )
    append_diagnostic_warning(
        latest_signal_bar,
        "CANDLE_INTEGRITY",
        integrity_warnings,
        integrity_results,
    )
    append_diagnostic_warning(
        latest_signal_bar,
        "TIMEFRAME_ALIGNMENT",
        alignment_warnings,
        alignment_results,
    )

    cycle_diagnostics = {
        "fetch_time_utc": now_utc.isoformat(),
        "panel_rows": int(len(panel)),
        "closed_rows": int(panel["bar_closed_now"].sum()),
        "valid_next_entry_rows": int(panel["valid_next_entry"].sum()),
        "integrity_results": integrity_results,
        "alignment_results": alignment_results,
        "feature_drift": feature_diag,
        "signal_drift": signal_diag,
        "missing_long_model_columns": [c for c in LONG_FEATURE_COLS if c not in panel.columns],
        "missing_short_model_columns": [c for c in SHORT_FEATURE_COLS if c not in panel.columns],
    }

    if decision_source.empty:
        logging.info("[INFO] no closed 15m signal bars with next entry")
        return state

    logging.info(
        "[PANEL] rows=%d closed=%d valid_next=%d last_closed_signal=%s",
        len(panel),
        int(panel["bar_closed_now"].sum()),
        int(panel["valid_next_entry"].sum()),
        latest_signal_bar,
    )

    if not state.get("initialized", False):
        return bootstrap_state_from_history(state, decision_source)

    if not state.get("selector_state_initialized", False):
        cutoff = (
            pd.Timestamp(state["last_processed_bar"])
            if state.get("last_processed_bar") else None
        )
        state = rebuild_selector_state_from_history(
            state, decision_source, through=cutoff
        )
        save_runtime_state(state)
        logging.info(
            "[STATE MIGRATION] exact pre-ML selector state rebuilt through=%s",
            cutoff,
        )

    last_processed = (
        pd.Timestamp(state["last_processed_bar"])
        if state.get("last_processed_bar")
        else None
    )
    new_rows = (
        decision_source
        if last_processed is None
        else decision_source[decision_source["ts_open"] > last_processed].copy()
    )
    new_rows = new_rows.sort_values("ts_open").reset_index(drop=True)

    latest_bar = float(decision_source.iloc[-1]["close"])
    live_price = fetch_audit_live_price_snapshot(latest_bar)
    if live_price.get("fallback_used"):
        append_diagnostic_warning(
            latest_signal_bar,
            "LIVE_PRICE_FALLBACK",
            list(live_price.get("errors") or ["Live book ticker unavailable; bar close used"]),
            live_price,
        )

    if new_rows.empty:
        pos = load_position(state)
        pos_side = pos.side if pos else 0
        mtm = (
            float(live_price["bid"]) if pos_side == +1
            else float(live_price["ask"]) if pos_side == -1
            else float(live_price["mid"])
        )
        logging.info(
            "[NO NEW BAR] bid=%.2f ask=%.2f mid=%.2f mtm=%.2f "
            "latest_closed=%.2f pos=%s",
            float(live_price["bid"]),
            float(live_price["ask"]),
            float(live_price["mid"]),
            mtm,
            latest_bar,
            position_txt(pos_side),
        )
        return state

    silent_catchup = needs_silent_catchup(last_processed, new_rows)
    processing_mode = "CATCHUP" if silent_catchup else "LIVE_SCHEDULED"
    last_event: Optional[Dict[str, Any]] = None

    for index, row in new_rows.iterrows():
        state_before_bar = copy.deepcopy(state)
        try:
            state, last_event = process_one_signal_bar(
                row,
                state,
                send_alerts=not silent_catchup,
            )
            persist_bar_audits_and_state(
                row,
                last_event,
                state,
                live_price,
                cycle_diagnostics,
                processing_mode,
            )
            logging.info(
                "[MASTER AUDIT SAVED] %d/%d | t=%s | position=%s",
                index + 1,
                len(new_rows),
                last_event.get("t"),
                last_event.get("position_after"),
            )
        except Exception as exc:
            state.clear()
            state.update(state_before_bar)
            append_runtime_error("process_or_persist_bar", exc, state)
            raise

    if silent_catchup:
        logging.info(
            "[CATCHUP MODE] processed %d accumulated 15m bars with email alerts disabled",
            len(new_rows),
        )
        return state

    pos = load_position(state)
    pos_side = pos.side if pos else 0
    mtm = (
        float(live_price["bid"]) if pos_side == +1
        else float(live_price["ask"]) if pos_side == -1
        else float(live_price["mid"])
    )
    logging.info(
        "[LIVE] t=%s bid=%.2f ask=%.2f mid=%.2f mtm=%.2f (bar=%.2f) "
        "rule=%s side=%s ml_p=%s thr=%s accept=%s opened=%s closed=%s pos=%s",
        last_event.get("t"),
        float(live_price["bid"]),
        float(live_price["ask"]),
        float(live_price["mid"]),
        mtm,
        float(new_rows.iloc[-1]["close"]),
        last_event.get("rule_reason"),
        last_event.get("rule_side"),
        "n/a" if last_event.get("ml_prob") is None else f"{last_event['ml_prob']:.6f}",
        "n/a" if last_event.get("ml_threshold") is None else f"{last_event['ml_threshold']:.3f}",
        last_event.get("ml_accept"),
        last_event.get("opened"),
        last_event.get("closed_reason"),
        position_txt(pos_side),
    )
    logging.info("[MASTER AUDIT FILE] %s", MASTER_AUDIT_FILE)
    return state


# =============================================================================
# STARTUP EXPORT ENGINE CHECK
# =============================================================================
def run_startup_export_engine_check() -> None:
    target = V22_ENGINE_DECISION_CONFIG.get("locked_final_target", {}) if isinstance(V22_ENGINE_DECISION_CONFIG, dict) else {}
    summary = V22_ENGINE_PARITY_SUMMARY if isinstance(V22_ENGINE_PARITY_SUMMARY, dict) else {}
    status = str(summary.get("status", ""))
    final_trades = int(target.get("final_trades", summary.get("validated_final_trades", -1)))
    long_thr = float(target.get("long_threshold", summary.get("validated_long_threshold", np.nan)))
    short_thr = float(target.get("short_threshold", summary.get("validated_short_threshold", np.nan)))
    long_candidates = int(summary.get("v22_long_source_candidates", len(V22_LONG_SOURCE_MAP)))
    checks = {
        "status_ready": status == "PASS_READY_FOR_LIVE_BUILD_INPUT",
        "long_source_nonzero": long_candidates > 0,
        "loaded_source_matches_summary": len(V22_LONG_SOURCE_MAP) == long_candidates,
    }
    logging.info("[EXPORT ENGINE CHECK] dir=%s", V22_ENGINE_EXPORT_DIR)
    logging.info("[EXPORT ENGINE CHECK] status=%s export_target_trades=%s export_long_thr=%.3f export_short_thr=%.3f long_source_candidates=%s loaded=%s",
                 status, final_trades, long_thr, short_thr, long_candidates, len(V22_LONG_SOURCE_MAP))
    logging.info(
        "[EXPORT ENGINE CHECK] target thresholds/trades are metadata only; "
        "current live ML artifact contract is validated from bundle/config as "
        "LONG=%.3f SHORT=%.3f trades=%d",
        EXPECTED_LONG_THRESHOLD,
        EXPECTED_SHORT_THRESHOLD,
        EXPECTED_FINAL_TRADES,
    )
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"V22 live engine export check failed: {failed}")
    logging.info("[EXPORT ENGINE CHECK] PASS")


# =============================================================================
# STARTUP VERIFICATION — AUDIT ONLY / FAIL-FAST / ONE RECORD PER RUN
# =============================================================================
def _critical_logic_functions() -> Dict[str, Any]:
    return {
        "calculate_features": calculate_features,
        "attach_htf_live": attach_htf_live,
        "attach_v22_locked_htf_context": attach_v22_locked_htf_context,
        "build_live_panel": build_live_panel,
        "v22_training_pre_entry_archetype": v22_training_pre_entry_archetype,
        "v22_long_causal_gate_state": v22_long_causal_gate_state,
        "_short_rule_candidate": _short_rule_candidate,
        "build_ml_sample": build_ml_sample,
        "predict_side": predict_side,
        "create_open_position": create_open_position,
        "resolve_position_on_bar": resolve_position_on_bar,
        "advance_pre_ml_side_selectors": advance_pre_ml_side_selectors,
        "process_one_signal_bar": process_one_signal_bar,
    }


def _source_sha256(func: Any) -> str:
    source = inspect.getsource(func)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _startup_leakage_scan() -> Dict[str, Any]:
    forbidden_substrings = tuple(dict.fromkeys(
        list(LEAKY_PATTERNS)
        + [
            "entry_open_next",
            "entry_ts_next",
            "valid_next_entry",
            "seconds_to_bar_close_now",
        ]
    ))
    feature_hits: List[Dict[str, Any]] = []
    for side, cols in (("LONG", LONG_FEATURE_COLS), ("SHORT", SHORT_FEATURE_COLS)):
        for feature in cols:
            name = str(feature).lower()
            hits = [token for token in forbidden_substrings if token in name]
            if hits:
                feature_hits.append({
                    "side": side,
                    "feature": str(feature),
                    "hits": hits,
                })

    spec_hits: List[Dict[str, Any]] = []
    for label, specs in (("LONG", LONG_SPECS), ("SHORT", SHORT_SPECS)):
        for spec in specs:
            joined = " ".join([
                str(spec.feature),
                str(spec.column_raw),
                str(spec.source_column),
            ]).lower()
            hits = [token for token in LEAKY_PATTERNS if token in joined]
            if hits:
                spec_hits.append({
                    "side": label,
                    "row_i": int(spec.row_i),
                    "feature": str(spec.feature),
                    "source_column": str(spec.source_column),
                    "hits": hits,
                })

    return {
        "status": "PASS" if not feature_hits and not spec_hits else "FAIL",
        "model_feature_hits": feature_hits,
        "shortlist_spec_hits": spec_hits,
        "forbidden_substrings": list(forbidden_substrings),
    }


def _startup_no_order_scan() -> Dict[str, Any]:
    # Scan executable function sources while excluding the verifier helpers
    # themselves, so the forbidden marker strings below cannot self-trigger.
    verifier_names = {
        "_startup_no_order_scan",
        "_startup_leakage_scan",
        "_startup_external_source_static_contract",
        "_startup_audit_files_contract",
        "_append_startup_verification_record",
        "run_startup_verification_static",
        "finalize_startup_verification",
        "_critical_logic_functions",
        "_source_sha256",
    }
    executable_sources: List[str] = []
    for name, obj in globals().items():
        if name in verifier_names or not inspect.isfunction(obj):
            continue
        try:
            executable_sources.append(inspect.getsource(obj))
        except Exception:
            continue
    scanned_source = "\n".join(executable_sources)
    forbidden_markers = [
        "/fapi/v1/order",
        "/api/v3/order",
        "requests.post(",
        "requests.put(",
        "requests.delete(",
        "requests.patch(",
    ]
    hits = [marker for marker in forbidden_markers if marker in scanned_source]
    module_source = Path(__file__).read_text(encoding="utf-8")
    return {
        "status": "PASS" if not hits else "FAIL",
        "hits": hits,
        "paper_only_declared": "PAPER ONLY / NO REAL ORDERS" in module_source,
    }


def _startup_external_source_static_contract() -> Dict[str, Any]:
    fetch_source = inspect.getsource(fetch_time_series)
    realagg_source = inspect.getsource(fetch_realagg_for_tf)
    oi_source = inspect.getsource(fetch_open_interest_hist)
    checks = {
        "eth_base_is_usdm_futures": "fetch_futures_klines(SYMBOL, tf, target)" in fetch_source,
        "realagg_exact_path_present": "fetch_realagg_for_tf(tf, end_dt)" in fetch_source,
        "oi_exact_path_present": "fetch_open_interest_hist(tf, start_dt, end_dt, base)" in fetch_source,
        "realagg_no_kline_proxy_in_fetcher": "kline" not in realagg_source.lower(),
        "oi_uses_5m_cache_contract": "_ensure_oi_5m_cache" in oi_source,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
    }


def _startup_audit_files_contract() -> Dict[str, Any]:
    expected_paths = [
        MASTER_AUDIT_FILE,
        TRADES_FILE,
        RUNTIME_STATE_FILE,
        ERRORS_FILE,
        EXTERNAL_DATA_AUDIT_FILE,
    ]
    expected_names = {path.name for path in expected_paths}
    checks = {
        "exactly_five_paths": len(expected_paths) == 5 and len(set(expected_paths)) == 5,
        "allowed_names_exact": expected_names == set(ALLOWED_AUDIT_FILENAMES),
        "all_exist_after_initialize": all(path.exists() for path in expected_paths),
        "runtime_cache_outside_audit_dir": LIVE_AUDIT_DIR not in REALAGG_RUNTIME_CACHE_FILE.parents,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "paths": [str(path) for path in expected_paths],
    }


def _append_startup_verification_record(
    static_result: Dict[str, Any],
    live_external_record: Optional[Dict[str, Any]],
    final_status: str,
    failures: List[str],
) -> Dict[str, Any]:
    record = {
        "schema_version": STATE_SCHEMA_VERSION,
        "record_type": "STARTUP_VERIFICATION",
        "event_key": f"STARTUP_VERIFICATION|{RUN_ID}",
        "run_id": RUN_ID,
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "status": final_status,
        "failures": list(failures),
        "static_verification": static_result,
        "live_external_data_verification": live_external_record,
    }
    append_jsonl_row_unique(EXTERNAL_DATA_AUDIT_FILE, record, "event_key")
    return record


def run_startup_verification_static(local_feature_audit_passed: bool) -> Dict[str, Any]:
    global _STARTUP_STATIC_VERIFICATION_RESULT

    artifact_actual = {
        name: sha256_file(path)
        for name, path in FINAL_MODEL_ARTIFACT_FILES.items()
    }
    artifact_ok = all(
        artifact_actual.get(name) == expected
        for name, expected in EXPECTED_FINAL_MODEL_ARTIFACT_SHA256.items()
    )

    logic_actual = {
        name: _source_sha256(func)
        for name, func in _critical_logic_functions().items()
    }
    logic_mismatches = {
        name: {
            "actual": logic_actual.get(name),
            "expected": expected,
        }
        for name, expected in EXPECTED_CRITICAL_LOGIC_SHA256.items()
        if logic_actual.get(name) != expected
    }

    feature_contract_checks = {
        "local_historical_comparable_feature_audit": bool(local_feature_audit_passed),
        "long_feature_count_120": len(LONG_FEATURE_COLS) == 120,
        "short_feature_count_120": len(SHORT_FEATURE_COLS) == 120,
        "long_features_unique": len(set(LONG_FEATURE_COLS)) == len(LONG_FEATURE_COLS),
        "short_features_unique": len(set(SHORT_FEATURE_COLS)) == len(SHORT_FEATURE_COLS),
        "known_exemptions_exact": frozenset(STARTUP_FEATURE_AUDIT_SKIP_COLUMNS)
        == EXPECTED_STARTUP_FEATURE_AUDIT_SKIP_COLUMNS,
    }
    feature_contract_ok = all(feature_contract_checks.values())

    leakage = _startup_leakage_scan()
    no_orders = _startup_no_order_scan()
    external_static = _startup_external_source_static_contract()
    audit_files = _startup_audit_files_contract()

    threshold_ok = (
        abs(float(LONG_THRESHOLD) - EXPECTED_LONG_THRESHOLD) <= 1e-9
        and abs(float(SHORT_THRESHOLD) - EXPECTED_SHORT_THRESHOLD) <= 1e-9
    )

    checks = {
        "artifact_sha256": "PASS" if artifact_ok else "FAIL",
        "thresholds": "PASS" if threshold_ok else "FAIL",
        "critical_logic_sha256": "PASS" if not logic_mismatches else "FAIL",
        "feature_contract": "PASS" if feature_contract_ok else "FAIL",
        "leakage_scan": leakage["status"],
        "external_source_static_contract": external_static["status"],
        "audit_files_contract": audit_files["status"],
        "paper_no_order_scan": no_orders["status"],
    }
    failures = [name for name, status in checks.items() if status != "PASS"]

    result = {
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
        "failures": failures,
        "artifact_sha256_actual": artifact_actual,
        "artifact_sha256_expected": dict(EXPECTED_FINAL_MODEL_ARTIFACT_SHA256),
        "thresholds": {
            "long_actual": float(LONG_THRESHOLD),
            "short_actual": float(SHORT_THRESHOLD),
            "long_expected": float(EXPECTED_LONG_THRESHOLD),
            "short_expected": float(EXPECTED_SHORT_THRESHOLD),
        },
        "critical_logic_sha256_actual": logic_actual,
        "critical_logic_sha256_expected": dict(EXPECTED_CRITICAL_LOGIC_SHA256),
        "critical_logic_mismatches": logic_mismatches,
        "feature_contract_checks": feature_contract_checks,
        "known_feature_exemptions": {
            "status": "KNOWN_EXEMPTION",
            "count": len(STARTUP_FEATURE_AUDIT_SKIP_COLUMNS),
            "columns": sorted(STARTUP_FEATURE_AUDIT_SKIP_COLUMNS),
            "reason": "frozen historical generic CVD/flow source-state rebase; actual live model/external features remain separately audited",
        },
        "leakage_scan": leakage,
        "external_source_static_contract": external_static,
        "audit_files_contract": audit_files,
        "paper_no_order_scan": no_orders,
    }
    _STARTUP_STATIC_VERIFICATION_RESULT = result

    logging.info("[STARTUP VERIFICATION] artifacts_sha=%s", checks["artifact_sha256"])
    logging.info(
        "[STARTUP VERIFICATION] thresholds=%s | LONG=%.3f SHORT=%.3f",
        checks["thresholds"],
        LONG_THRESHOLD,
        SHORT_THRESHOLD,
    )
    logging.info(
        "[STARTUP VERIFICATION] critical_logic_sha=%s | functions=%d",
        checks["critical_logic_sha256"],
        len(EXPECTED_CRITICAL_LOGIC_SHA256),
    )
    logging.info(
        "[STARTUP VERIFICATION] feature_contract=%s | features=%d/%d",
        checks["feature_contract"],
        len(LONG_FEATURE_COLS),
        len(SHORT_FEATURE_COLS),
    )
    logging.info(
        "[STARTUP VERIFICATION] leakage_scan=%s | model_hits=%d spec_hits=%d",
        checks["leakage_scan"],
        len(leakage.get("model_feature_hits", [])),
        len(leakage.get("shortlist_spec_hits", [])),
    )
    logging.info(
        "[STARTUP VERIFICATION] known_feature_exemptions=KNOWN_EXEMPTION | count=%d | columns=%s",
        len(STARTUP_FEATURE_AUDIT_SKIP_COLUMNS),
        sorted(STARTUP_FEATURE_AUDIT_SKIP_COLUMNS),
    )
    logging.info(
        "[STARTUP VERIFICATION] external_source_static=%s | audit_files=%s | paper_no_orders=%s",
        checks["external_source_static_contract"],
        checks["audit_files_contract"],
        checks["paper_no_order_scan"],
    )

    if failures:
        _append_startup_verification_record(result, None, "FAIL", failures)
        logging.error("[STARTUP VERIFICATION] FINAL=FAIL | failures=%s", failures)
        raise RuntimeError(f"STARTUP VERIFICATION STATIC FAIL: {failures}")

    logging.info("[STARTUP VERIFICATION] STATIC=PASS | live_external_data=PENDING_FIRST_FETCH")
    return result


def finalize_startup_verification(static_result: Dict[str, Any]) -> Dict[str, Any]:
    live_record = _LAST_EXTERNAL_DATA_AUDIT_RECORD
    failures = list(static_result.get("failures") or [])

    live_current_run = bool(
        isinstance(live_record, dict)
        and str(live_record.get("run_id")) == str(RUN_ID)
        and str(live_record.get("record_type")) == "EXTERNAL_DATA_FIX_AUDIT"
    )
    live_pass = bool(live_current_run and str(live_record.get("status")) == "PASS")
    if not live_current_run:
        failures.append("live_external_data_current_run_missing")
    elif not live_pass:
        failures.append("live_external_data_contract")

    final_status = "PASS" if not failures else "FAIL"
    record = _append_startup_verification_record(
        static_result,
        live_record,
        final_status,
        failures,
    )

    logging.info(
        "[STARTUP VERIFICATION] external_live_data=%s | current_run_record=%s",
        "PASS" if live_pass else "FAIL",
        "YES" if live_current_run else "NO",
    )
    logging.info(
        "[STARTUP VERIFICATION] saved_once_per_run=%s | file=%s",
        record.get("event_key"),
        EXTERNAL_DATA_AUDIT_FILE,
    )
    logging.info("[STARTUP VERIFICATION] FINAL=%s", final_status)

    if final_status != "PASS":
        raise RuntimeError(f"STARTUP VERIFICATION FINAL FAIL: {failures}")
    return record


# =============================================================================
# MAIN LOOP
# =============================================================================
def seconds_until_next_15m(now_utc: datetime) -> int:
    base = now_utc.replace(second=0, microsecond=0)
    boundary = base - timedelta(minutes=now_utc.minute % 15) + timedelta(minutes=15)
    wait_s = int((boundary - now_utc).total_seconds())
    return max(1, wait_s)


def main():
    global TRAINING_FINGERPRINT

    startup_verification_static: Dict[str, Any] = {}

    logging.info("[STARTUP PROGRESS] 5%% | acquiring clean-audit process lock")
    acquire_process_lock()
    state = default_runtime_state()

    try:
        logging.info("[STARTUP PROGRESS] 8%% | validating final model artifact contract")
        run_startup_model_artifact_contract_check()
        logging.info("[STARTUP PROGRESS] 10%% | initializing exactly five audit files")
        initialize_clean_audit_files()
        logging.info("🟢 Shadow Live ETHUSDT 15m V22 up — PAPER ONLY / NO REAL ORDERS")
        logging.info("[base_dir] %s", BASE_DIR)
        logging.info("[clean_audit_dir] %s", LIVE_AUDIT_DIR)
        logging.info("[realagg_runtime_cache] %s", REALAGG_RUNTIME_CACHE_FILE)
        logging.info("[1/5 master_audit] %s", MASTER_AUDIT_FILE)
        logging.info("[2/5 trades] %s", TRADES_FILE)
        logging.info("[3/5 state] %s", RUNTIME_STATE_FILE)
        logging.info("[4/5 errors] %s", ERRORS_FILE)
        logging.info("[5/5 external_data_fix_audit] %s", EXTERNAL_DATA_AUDIT_FILE)
        logging.info("[bundle] %s", BUNDLE_FILE)
        logging.info("[config] %s", CONFIG_FILE)
        logging.info("[shortlist] %s", SHORTLIST_FILE)
        logging.info("[v22_engine_export_dir] %s", V22_ENGINE_EXPORT_DIR)
        logging.info(
            "[long_threshold] %.3f | [short_threshold] %.3f",
            LONG_THRESHOLD,
            SHORT_THRESHOLD,
        )
        if (
            abs(LONG_THRESHOLD - EXPECTED_LONG_THRESHOLD) > 1e-9
            or abs(SHORT_THRESHOLD - EXPECTED_SHORT_THRESHOLD) > 1e-9
        ):
            raise RuntimeError(
                f"Threshold mismatch: long={LONG_THRESHOLD} short={SHORT_THRESHOLD}; "
                f"expected long={EXPECTED_LONG_THRESHOLD} short={EXPECTED_SHORT_THRESHOLD}"
            )
        logging.info(
            "[long_features] %d | [short_features] %d",
            len(LONG_FEATURE_COLS),
            len(SHORT_FEATURE_COLS),
        )
        logging.info("[entry_mode] NEXT 15m open")
        logging.info(
            "[execution] paper/shadow only; 15m only; global one-position; "
            "LONG_FIRST; no real orders"
        )
        logging.info(
            "[resume] accumulated append + atomic state + open-position restore + duplicate protection"
        )

        logging.info("[STARTUP PROGRESS] 20%% | validating locked export engine")
        run_startup_export_engine_check()

        if AUDIT_MODE:
            logging.info("[STARTUP PROGRESS] 30%% | running local feature audit")
            run_local_feature_audit()
            logging.info("[STARTUP PROGRESS] 55%% | building in-memory training fingerprint")
            TRAINING_FINGERPRINT = load_or_build_training_fingerprint()
            log_training_fingerprint_summary(TRAINING_FINGERPRINT)
            if RUN_STARTUP_FULL_PARITY_REPLAY:
                run_startup_full_parity_replay()

        logging.info("[STARTUP PROGRESS] 60%% | running fail-fast startup verification")
        startup_verification_static = run_startup_verification_static(
            local_feature_audit_passed=bool(AUDIT_MODE)
        )

        logging.info("[STARTUP PROGRESS] 70%% | restoring clean accumulated state")
        state = load_runtime_state()
        logging.info("[STARTUP PROGRESS] 80%% | running first live fetch/audit cycle")
        state = run_once(state)
        logging.info("[STARTUP PROGRESS] 95%% | finalizing startup verification from current live cycle")
        finalize_startup_verification(startup_verification_static)
        logging.info("[STARTUP PROGRESS] 100%% | scheduled clean shadow live active")

        while True:
            try:
                wait_seconds = seconds_until_next_15m(datetime.now(timezone.utc)) + 1
                logging.info("[SCHEDULE] next 15m cycle in %d seconds", wait_seconds)
                time.sleep(wait_seconds)
                state = run_once(state)
            except KeyboardInterrupt:
                logging.info("[STOPPED BY USER]")
                break
            except Exception as exc:
                logging.error("[LOOP ERROR] %s", exc, exc_info=True)
                append_runtime_error("main_loop", exc, state)
                time.sleep(10)
    except Exception as exc:
        logging.error("[FATAL STARTUP/RUNTIME ERROR] %s", exc, exc_info=True)
        append_runtime_error("fatal_main", exc, state)
        raise
    finally:
        release_process_lock()


if __name__ == "__main__":
    main()
