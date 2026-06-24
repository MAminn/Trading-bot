#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETHUSDT — LIVE 15M V22 EXACT TRAINING MATCH FINAL / V22_LONG + SHORT_NO_FILTER + MANDATORY ML
==============================================================

Paper/live monitor only. No real orders.
Matched to final training artifacts:
- BASE_DIR: /Users/omarhassan/Desktop/project/Eth/test backup
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

Live audit outputs are written/appended cumulatively under:
- /Users/omarhassan/Desktop/project/Eth/test backup/model files/live_audit_v22_global_no_overlap
"""

from __future__ import annotations

import os
import json
import time
import smtplib
import warnings
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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
BASE_DIR = Path("/Users/omarhassan/Desktop/project/Eth/test backup")
ARTIFACTS_DIR = BASE_DIR / "model files"
LIVE_AUDIT_DIR = BASE_DIR / "model files" / "live_audit_v22_global_no_overlap"
LIVE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

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

LEVERAGE_SCENARIOS = [
    {"scenario": "conservative", "leverage": 1.0, "capital_usd": 80.0},
    {"scenario": "middle", "leverage": 30.0, "capital_usd": 80.0},
    {"scenario": "aggressive", "leverage": 70.0, "capital_usd": 80.0},
]

# All live outputs append cumulatively under live audit.
SIGNAL_MONITOR_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_live_signal_monitor.csv"
TRADE_LOG_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_live_trade_log.csv"
DIAGNOSTIC_STATUS_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_live_diagnostic_status.csv"

AUDIT_MODE = True
AUDIT_STRICT = True
AUDIT_TOL = 1e-6
AUDIT_ONLY_ON_STARTUP = True
RUN_STARTUP_FULL_PARITY_REPLAY = False
EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES = 3626
EXPECTED_FINAL_TRADES = EXPECTED_FINAL_GLOBAL_NO_OVERLAP_TRADES
EXPECTED_LONG_THRESHOLD = 0.400
EXPECTED_SHORT_THRESHOLD = 0.390
FINGERPRINT_REPLAY_BARS = 20000
LIVE_MONITOR_WINDOW = 200
FEATURE_STD_RATIO_MIN = 0.35
FEATURE_STD_RATIO_MAX = 2.50
RATE_WARN_ABS = 0.05
RATE_WARN_REL = 0.35
LOW_VAR_STD_EPS = 1e-10
DRIFT_WINDOW = 20

FINGERPRINT_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_training_fingerprint.json"
FINGERPRINT_HISTORY_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_training_fingerprint_history.jsonl"
AUDIT_SAMPLE_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_live_sample_audit.csv"
ROOT_DEBUG_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_root_debug.csv"
RULE_FUNNEL_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_rule_funnel.csv"
SHADOW_PARITY_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_shadow_parity.csv"
AUDIT_STATUS_FILE = LIVE_AUDIT_DIR / "ethusdt_15m_version_b_live_audit_status.csv"
TRAINING_FINGERPRINT = None

EMAIL_ADDRESS = os.getenv("LIVE_EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("LIVE_EMAIL_APP_PASSWORD", "")


# =============================================================================
# LOAD TRAINING ARTIFACTS
# =============================================================================
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
if abs(LONG_THRESHOLD - 0.400) > 1e-9 or abs(SHORT_THRESHOLD - 0.390) > 1e-9:
    raise RuntimeError(f"Live threshold mismatch: expected LONG=0.400 SHORT=0.390, got LONG={LONG_THRESHOLD:.3f} SHORT={SHORT_THRESHOLD:.3f}")


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
# FIXED CSV SCHEMAS — AUDIT/LOGGING ONLY
# =============================================================================
TRADE_LOG_COLUMNS = [
    "logged_at_utc", "trade_id", "side", "setup_name", "signal_t", "entry_t", "exit_t",
    "entry", "exit", "tp", "sl", "final_stop", "atr", "bars_held",
    "prob", "threshold", "exit_reason", "net_pnl_rate_after_round_trip_cost",
    "round_trip_cost", "leverage_scenarios_json",
]

SHADOW_PARITY_COLUMNS = [
    "t", "rule_reason", "rule_side", "ml_prob", "ml_threshold", "ml_accept",
    "opened", "closed_reason", "position_before", "position_after",
    "bar_closed_now", "valid_next_entry",
]

SIGNAL_MONITOR_COLUMNS = [
    "t", "bar_closed_now", "valid_next_entry", "rule_side", "rule_reason",
    "ml_prob", "ml_threshold", "ml_accept", "opened", "closed_reason",
    "position_before", "position_after", "event_json",
]

RULE_FUNNEL_COLUMNS = [
    "audit_time_utc", "signal_bar_utc", "funnel_side", "rule_reason", "rule_side",
    "opened", "closed_reason", "position_before", "position_after",
    "bar_closed_now", "valid_next_entry",
    "setup", "trigger", "valid_signal_row", "family_pass", "trigger_pass",
    "one_h_soft_veto_pass", "final_filter_pass", "final_filter_name",
    "ml_reached", "ml_status", "ml_accept", "ml_prob", "ml_threshold",
    "close", "open", "range", "body_pct", "mom", "s1_mom", "vol_z_20", "hour",
    "details_json",
]

SAMPLE_AUDIT_COLUMNS = [
    "audit_time_utc", "signal_bar_utc", "rule_reason", "rule_side",
    "ml_prob", "ml_threshold", "ml_accept", "bar_closed_now",
    "valid_next_entry", "sample_features_json",
]

ROOT_DEBUG_COLUMNS = [
    "logged_at_utc", "t", "bar_closed_now", "valid_next_entry", "rule_side",
    "rule_reason", "ml_prob", "ml_threshold", "ml_accept", "opened",
    "closed_reason", "position_before", "position_after", "event_json",
]

DIAGNOSTIC_STATUS_COLUMNS = [
    "logged_at_utc", "rows", "last_ts_open", "closed_rows", "valid_next_entry_rows",
    "missing_long_model_cols_in_panel", "missing_short_model_cols_in_panel",
]

AUDIT_STATUS_COLUMNS = [
    "logged_at_utc", "rows", "last_ts_open", "feature_status", "feature_warnings",
    "signal_status", "signal_warnings", "feature_warn_n", "signal_warn_n",
    "missing_long_model_cols_in_panel", "missing_short_model_cols_in_panel",
]
for _tf_schema in TRAINING_AUDIT_TFS:
    AUDIT_STATUS_COLUMNS.extend([
        f"candle_{_tf_schema}_status",
        f"align_{_tf_schema}_status",
        f"candle_{_tf_schema}_bad_bounds",
        f"align_{_tf_schema}_misaligned",
    ])


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

def append_jsonl_row(path: Path, row: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(to_jsonable(row), ensure_ascii=False) + "\n")


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
    missing = [c for c in RAW_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{tf} missing raw columns: {missing}")
    df = df[RAW_COLUMNS].copy()
    for col in RAW_COLUMNS:
        if col != "date":
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
    f["binance_funding_rate_abs"] = pd.to_numeric(df["funding_rate"], errors="coerce").abs()
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


def load_training_panel_for_thresholds() -> pd.DataFrame:
    panel = load_historical_tf("15m")
    for tf in HTF_TFS:
        panel = attach_htf_training_style(panel, load_historical_tf(tf), tf)
    panel = panel[(panel["ts_open"] >= START_DATE) & (panel["ts_open"] <= END_DATE)].copy()
    panel = panel.sort_values("ts_open").reset_index(drop=True)
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
        vol_q33=qtrain(panel, splits, v22_vol_col, 0.33, False),
        vol_q66=qtrain(panel, splits, v22_vol_col, 0.66, False),
    )
    logging.info("[THRESHOLDS] loaded | long_specs=%d short_specs=%d", len(long_specs), len(short_specs))
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


def _spot_klines_request(symbol: str, interval: str, limit: int, end_time: Optional[int] = None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = int(end_time)
    return _request_json(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=30)


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
    start_ms = int(pd.Timestamp(start_dt).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end_dt).timestamp() * 1000)
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


def attach_external_columns(spot: pd.DataFrame, premium: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    df = spot.copy().sort_values("date").reset_index(drop=True)
    premium = premium.copy().sort_values("date").reset_index(drop=True)
    df = pd.merge_asof(df, premium, on="date", direction="backward", allow_exact_matches=True)
    funding = funding.copy().sort_values("funding_time").reset_index(drop=True)
    df = pd.merge_asof(df, funding, left_on="date", right_on="funding_time", direction="backward", allow_exact_matches=True)
    df = df.drop(columns=["funding_time"], errors="ignore")
    df["buy_base_volume"] = df["taker_buy_base_volume"]
    df["sell_base_volume"] = df["volume"] - df["taker_buy_base_volume"]
    df["buy_quote_volume"] = df["taker_buy_quote_volume"]
    df["sell_quote_volume"] = df["quote_asset_volume"] - df["taker_buy_quote_volume"]
    df["agg_trade_count"] = df["number_of_trades"]
    df["trade_flow_imbalance_base"] = df["buy_base_volume"] - df["sell_base_volume"]
    df["trade_flow_imbalance_quote"] = df["buy_quote_volume"] - df["sell_quote_volume"]
    df["cvd_base"] = df["trade_flow_imbalance_base"].fillna(0).cumsum()
    df["cvd_quote"] = df["trade_flow_imbalance_quote"].fillna(0).cumsum()
    for c in RAW_COLUMNS:
        if c not in df.columns:
            raise RuntimeError(f"[RAW BUILD] missing column after external attach: {c}")
    return df[RAW_COLUMNS].replace([np.inf, -np.inf], np.nan)


def fetch_time_series(tf: str) -> pd.DataFrame:
    target = OUTPUTSIZE[tf]
    spot = fetch_spot_klines(SYMBOL, tf, target)
    premium = fetch_premium_klines(tf, target)
    funding = fetch_funding_rates(spot["date"].min() - pd.Timedelta(days=2), spot["date"].max() + pd.Timedelta(days=1))
    return attach_external_columns(spot, premium, funding)


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
        btc = fetch_spot_klines("BTCUSDT", tf, target)
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
    base = base.sort_values("ts_close").reset_index(drop=True).copy()
    htf = htf.sort_values("ts_close").reset_index(drop=True).copy()
    reserved = {"ts_open", "ts_close", "entry_time_next", "entry_ts_next", "entry_open_next", "next_index", "valid_next_entry", "entry_gap_minutes"}
    rename = {c: f"{tf}__{c}" for c in htf.columns if c not in reserved and c != "date"}
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
    return out


def prepare_feature_frame(raw: pd.DataFrame, tf: str, add_context_features: bool = True) -> pd.DataFrame:
    df = calculate_features(raw, tf)
    if add_context_features:
        df = attach_btc_ethbtc_context(df, tf)
        df = add_final_extra_features(df, tf)
    df["ts_open"] = to_utc(df["date"])
    df["ts_close"] = df["ts_open"] + pd.to_timedelta(tf_minutes(tf), unit="m")
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

    panel = rebuild_execution_columns(panel)
    panel["bar_closed_now"] = panel["ts_close"] <= pd.Timestamp(now_utc)
    panel["seconds_to_bar_close_now"] = (panel["ts_close"] - pd.Timestamp(now_utc)).dt.total_seconds().clip(lower=0.0)

    for tf in HTF_TFS:
        c = f"{tf}__ts_close"
        if c in panel.columns:
            panel[f"age_{tf}_min"] = (panel["ts_close"] - panel[c]).dt.total_seconds() / 60.0

    needed_cols = set(LONG_FEATURE_COLS) | set(SHORT_FEATURE_COLS) | {"s1_mom"}
    for c in needed_cols:
        if c not in panel.columns:
            panel[c] = np.nan

    return panel.replace([np.inf, -np.inf], np.nan)


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
    th = V22_THRESHOLDS
    close_pos = row_num_any(row, ["close_pos", "close_position"], 0.0)
    range_pct = row_num_any(row, ["range_pct"], 0.0)
    ret4 = row_num_any(row, ["ret4"], 0.0)
    ret12 = row_num_any(row, ["ret12"], 0.0)
    ret24 = row_num_any(row, ["ret24"], 0.0)
    lower_wick_pct = row_num_any(row, ["lower_wick_pct"], 0.0)
    bb_bw = row_num_any(row, ["bb_bw"], np.inf)
    bb_z = row_num_any(row, ["bb_z"], 0.0)
    dist_ema20_atr = row_num_any(row, ["dist_ema20_atr"], 0.0)
    ema20_slope_10 = row_num_any(row, ["ema20_slope_10"], 0.0)
    close = row_num_any(row, ["close"], 0.0)
    prev_high_20 = row_num_any(row, ["prev_high_20"], np.inf)

    h1_up = (
        row_num_any(row, ["ema20_slope_10_1h", "1h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["trend_regime_ema50_200_1h", "1h__trend_regime_ema50_200"], 0.0) > 0
    )
    h4_up = (
        row_num_any(row, ["ema20_slope_10_4h", "4h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["trend_regime_ema50_200_4h", "4h__trend_regime_ema50_200"], 0.0) > 0
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
        row_num_any(row, ["realagg_buy_ratio_quote"], 0.5) >= th.q_realagg70
        or row_num_any(row, ["realagg_cvd_quote_delta_z_50"], 0.0) >= th.q_realagg_delta65
        or row_num_any(row, ["realagg_cvd_quote_delta_sum_4"], 0.0) > 0
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


def v22_live_long_candidate_approx_disabled(row: pd.Series) -> bool:
    if not valid_signal_row(row):
        return False
    th = V22_THRESHOLDS
    arch = v22_training_pre_entry_archetype(row)
    setup_ok = arch == "breakout"
    atrp = row_num_any(row, ["atrp_14", "pre_atr14_pct"], np.nan)
    range_pct = row_num_any(row, ["range_pct", "pre_range_pct"], 0.0)
    funding_abs = row_num_any(row, ["binance_funding_rate_abs", "pre_binance_funding_rate_abs"], 0.0)
    vol_ok = (pd.notna(atrp) and th.atr_low <= atrp <= th.atr_high) or (range_pct >= th.range_high)
    session_ok = row_num_any(row, ["session_active_07_21", "pre_session_active_07_21"], 1.0) >= 0.5
    htf_ok = (
        row_num_any(row, ["ema20_slope_10_1h", "1h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["ema20_slope_10_4h", "4h__ema20_slope_10"], 0.0) > 0
        or row_num_any(row, ["trend_regime_ema50_200_4h", "4h__trend_regime_ema50_200"], 0.0) > 0
        or row_num_any(row, ["trend_regime_ema50_200_1d", "1d__trend_regime_ema50_200"], 0.0) > 0
    )
    btc_ethbtc_ok = row_num_any(row, ["btc_trend_score"], 0.0) >= -1 and row_num_any(row, ["ethbtc_trend_score"], 0.0) >= -3
    flow_or_oi_ok = (
        row_num_any(row, ["realagg_buy_ratio_quote"], 0.5) >= 0.48
        or row_num_any(row, ["realagg_cvd_quote_delta_z_50"], 0.0) >= -0.25
        or row_num_any(row, ["oi_price_oi_divergence_4"], 0.0) >= 0
    )
    funding_ok = funding_abs <= th.funding_abs_hi
    return bool(setup_ok and vol_ok and session_ok and htf_ok and btc_ethbtc_ok and flow_or_oi_ok and funding_ok)


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


def build_rule_funnel(row: pd.Series) -> Dict[str, Dict[str, Any]]:
    valid = valid_signal_row(row)

    long_family_pass = bool(v22_live_long_candidate(row)) if valid else False
    long_trigger_pass = long_family_pass
    long_1h_soft_veto = True if valid else False
    long_final_filter = True if valid else False
    long_ml_reached = bool(valid and long_family_pass)

    short_family_pass = bool(family_setup_pass(row, SHORT_SPECS, "SHORT", SHORT_SETUP_FAMILY)) if valid else False
    short_trigger_pass = bool(short_momentum_break_trigger(row)) if valid else False
    short_1h_soft_veto = bool(short_1h_soft_veto_pass(row)) if valid else False
    short_final_filter = True if valid else False
    short_ml_reached = bool(valid and short_family_pass and short_trigger_pass and short_1h_soft_veto and short_final_filter)

    return {
        "long": {
            "side": "LONG",
            "setup": LONG_SETUP_NAME,
            "trigger": LONG_TRIGGER,
            "valid_signal_row": bool(valid),
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
            "v22_archetype": v22_training_pre_entry_archetype(row) if valid else "invalid",
        },
        "short": {
            "side": "SHORT",
            "setup": SHORT_SETUP_NAME,
            "trigger": SHORT_TRIGGER,
            "valid_signal_row": bool(valid),
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


def evaluate_rule_candidates(row: pd.Series) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not valid_signal_row(row):
        return out
    v22_meta = v22_long_source_meta(row)
    if v22_live_long_candidate(row):
        out.append({"side": +1, "setup": LONG_SETUP_NAME, "trigger": LONG_TRIGGER, "exit": LONG_EXIT_NAME, "v22_source_meta": v22_meta})
    if family_setup_pass(row, SHORT_SPECS, "SHORT", SHORT_SETUP_FAMILY) and short_momentum_break_trigger(row) and short_1h_soft_veto_pass(row):
        out.append({"side": -1, "setup": SHORT_SETUP_NAME, "trigger": SHORT_TRIGGER, "exit": SHORT_EXIT_NAME})
    return out


# =============================================================================
# ML — SEPARATE LONG / SHORT MODELS
# =============================================================================
def build_ml_sample(row: pd.Series, feature_cols: List[str], entry_time: pd.Timestamp) -> Dict[str, Any]:
    sample: Dict[str, Any] = {}
    for c in feature_cols:
        if c == "ml_hour":
            sample[c] = int(entry_time.hour)
        elif c == "ml_dow":
            sample[c] = int(entry_time.dayofweek)
        elif c == "ml_is_us_session":
            sample[c] = 1.0 if 13 <= int(entry_time.hour) < 22 else 0.0
        elif c == "ml_is_london_session":
            sample[c] = 1.0 if 7 <= int(entry_time.hour) < 13 else 0.0
        elif c == "ml_is_asia_session":
            sample[c] = 1.0 if 0 <= int(entry_time.hour) < 7 else 0.0
        elif c in row.index:
            sample[c] = row.get(c)
        else:
            sample[c] = np.nan
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
    oi = row_num_any(row, ["pre_oi_open_interest_z_20", "oi_open_interest_z_20", "oi_open_interest_z_96", "oi_open_interest_value_z_96"], np.nan)
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


def v22_rx4_class_from_live(pos: OpenPosition, close: float) -> str:
    mfe = (float(pos.best_high) - float(pos.entry)) / max(float(pos.atr), 1e-12)
    mae = (float(pos.entry) - float(pos.best_low)) / max(float(pos.atr), 1e-12)
    close_atr = (float(close) - float(pos.entry)) / max(float(pos.atr), 1e-12)
    if mfe >= 1.0 and close_atr >= 0.0 and mae <= 1.0:
        return "rx4_runner_like"
    if mfe <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close_atr <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR and mae >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR:
        return "rx4_true_failure_like"
    if mfe >= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close_atr > V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR:
        return "rx4_false_sl_like"
    return "rx4_mixed"


def resolve_v22_long_position_on_bar(pos: OpenPosition, row: pd.Series) -> Tuple[Optional[float], Optional[str], OpenPosition]:
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
            mfe = (float(pos.best_high) - float(pos.entry)) / max(float(pos.atr), 1e-12)
            mae = (float(pos.entry) - float(pos.best_low)) / max(float(pos.atr), 1e-12)
            close_atr = (cl - float(pos.entry)) / max(float(pos.atr), 1e-12)
            rx4 = v22_rx4_class_from_live(pos, cl)
            true_fail = mfe <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close_atr <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR and mae >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR
            mixed_bad = rx4 == "rx4_mixed" and close_atr <= V22_MIXED_BAD_CLOSE_MAX_ATR and mfe <= V22_MIXED_BAD_MFE_MAX_ATR and mae >= V22_MIXED_BAD_MAE_MIN_ATR
            if true_fail:
                return cl, "V22_TRUE_FAILURE_EXIT", pos
            if mixed_bad:
                return cl, "V22_MIXED_BAD_EXIT", pos
        pos.bars_held += 1
        return None, None, pos

    rx4 = v22_rx4_class_from_live(pos, cl)
    if rx4 == "rx4_false_sl_like":
        normal_sl_atr, trail_start_atr, trail_dist_atr = 1.55, V22_BASE_TRAIL_START_ATR, V22_BASE_TRAIL_DIST_ATR
    elif rx4 == "rx4_mixed" and bar_no <= 8:
        mfe = (float(pos.best_high) - float(pos.entry)) / max(float(pos.atr), 1e-12)
        close_atr = (cl - float(pos.entry)) / max(float(pos.atr), 1e-12)
        if mfe >= V22_MIXED_RECOVER_MFE_MIN_ATR or close_atr >= V22_MIXED_RECOVER_CLOSE_MIN_ATR:
            normal_sl_atr, trail_start_atr, trail_dist_atr = V22_MIXED_RECOVER_SL_ATR, V22_MIXED_RECOVER_TRAIL_START_ATR, V22_MIXED_RECOVER_TRAIL_DIST_ATR
        else:
            normal_sl_atr, trail_start_atr, trail_dist_atr = 1.75, 0.85, 0.45
    else:
        normal_sl_atr, trail_start_atr, trail_dist_atr = V22_BASE_NORMAL_SL_ATR, V22_BASE_TRAIL_START_ATR, V22_BASE_TRAIL_DIST_ATR

    normal_sl = float(pos.entry) - normal_sl_atr * float(pos.atr)
    tp_price = float(pos.entry) + V22_BASE_TP_ATR * float(pos.atr)
    if not pos.trail_active and float(pos.best_high) >= float(pos.entry) + trail_start_atr * float(pos.atr):
        pos.trail_active = True
        pos.stop = float(pos.best_high) - trail_dist_atr * float(pos.atr)
    elif pos.trail_active:
        pos.stop = max(float(pos.stop), float(pos.best_high) - trail_dist_atr * float(pos.atr))
    if lo <= normal_sl:
        return normal_sl, "V22_NORMAL_SL", pos
    if pos.trail_active and lo <= float(pos.stop):
        return float(pos.stop), "V22_TRAIL", pos
    if hi >= tp_price:
        return tp_price, "V22_TP", pos
    pos.bars_held += 1
    if pos.bars_held >= V22_BASE_MAX_HOLD_BARS:
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



def append_closed_trade(pos: OpenPosition, exit_t: pd.Timestamp, exit_px: float, reason: str):
    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "trade_id": pos.trade_id,
        "side": position_txt(pos.side),
        "setup_name": pos.setup_name,
        "signal_t": pos.signal_t,
        "entry_t": pos.entry_t,
        "exit_t": exit_t.isoformat(),
        "entry": float(pos.entry),
        "exit": float(exit_px),
        "tp": float(pos.tp),
        "sl": float(pos.sl),
        "final_stop": float(pos.stop),
        "atr": float(pos.atr),
        "bars_held": int(pos.bars_held),
        "prob": float(pos.prob),
        "threshold": float(pos.threshold),
        "exit_reason": reason,
        "net_pnl_rate_after_round_trip_cost": trade_pnl(pos, exit_px),
        "round_trip_cost": ROUND_TRIP_COST,
        "leverage_scenarios_json": _safe_json(build_close_leverage_scenarios(pos, exit_px)),
    }
    append_csv_row(TRADE_LOG_FILE, row, TRADE_LOG_COLUMNS)

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
# STATE / BAR PROCESSING
# =============================================================================
def default_runtime_state() -> Dict[str, Any]:
    return {"initialized": False, "last_processed_bar": None, "position": None}


def load_position(state: Dict[str, Any]) -> Optional[OpenPosition]:
    if state.get("position") is None:
        return None
    return OpenPosition(**state["position"])


def save_position(state: Dict[str, Any], pos: Optional[OpenPosition]) -> None:
    state["position"] = asdict(pos) if pos is not None else None


def process_one_signal_bar(row: pd.Series, state: Dict[str, Any], send_alerts: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    pos = load_position(state)
    event: Dict[str, Any] = {
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
        "position_before": position_txt(pos.side if pos is not None else 0),
        "position_after": None,
        "leverage_scenarios_json": None,
        "sample_features": None,
        "rule_funnel": build_rule_funnel(row),
    }
    if pos is not None:
        entry_t = pd.Timestamp(pos.entry_t)
        bar_t = pd.Timestamp(row["ts_open"])
        if bar_t >= entry_t:
            exit_px, reason, pos = resolve_position_on_bar(pos, row)
            if exit_px is not None and reason is not None:
                exit_t = pd.Timestamp(row["ts_close"])
                if send_alerts:
                    append_closed_trade(pos, exit_t, float(exit_px), reason)
                    send_close_email(pos, exit_t, float(exit_px), reason)
                event["closed_reason"] = reason
                event = add_close_leverage_columns(event, pos, float(exit_px))
                pos = None
    if pos is None:
        candidates = evaluate_rule_candidates(row)
        scored = []
        for cand in candidates:
            side = int(cand["side"])
            entry_time = pd.Timestamp(row["entry_ts_next"])
            accept, prob, thr, calibration, sample = predict_side(side, row, entry_time)
            funnel_key = "long" if side == +1 else "short"
            event["rule_funnel"][funnel_key]["ml_prob"] = float(prob)
            event["rule_funnel"][funnel_key]["ml_threshold"] = float(thr)
            event["rule_funnel"][funnel_key]["ml_accept"] = bool(accept)
            event["rule_funnel"][funnel_key]["ml_status"] = "ML_ACCEPT" if accept else "ML_REJECT"
            scored.append({"candidate": cand, "accept": accept, "prob": prob, "thr": thr, "calibration": calibration, "sample": sample})
        accepted = [x for x in scored if x["accept"]]
        if accepted:
            # Match final training portfolio policy: global one-position, no flip, LONG_FIRST same-bar priority.
            accepted.sort(key=lambda x: (0 if int(x["candidate"]["side"]) == +1 else 1, -(x["prob"] - x["thr"]), -x["prob"]))
            best = accepted[0]
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
            best = sorted(scored, key=lambda x: x["prob"], reverse=True)[0]
            cand = best["candidate"]
            event.update({
                "rule_side": int(cand["side"]),
                "rule_reason": f"{cand['setup']}|{cand['trigger']}|ML_REJECT",
                "ml_prob": float(best["prob"]),
                "ml_threshold": float(best["thr"]),
                "ml_accept": False,
                "sample_features": dict(best["sample"]),
            })
    save_position(state, pos)
    state["last_processed_bar"] = pd.Timestamp(row["ts_open"]).isoformat()
    event["position_after"] = position_txt(pos.side if pos is not None else 0)
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
    raw = hist[RAW_COLUMNS].copy()
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    for c in RAW_COLUMNS:
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
        compare_cols = [c for c in BASE_FEATURES if c in hist.columns and c in calc.columns]
        skipped_features = [c for c in BASE_FEATURES if c not in hist.columns or c not in calc.columns]
        if skipped_features:
            logging.warning("[AUDIT] %s: skipped unavailable feature columns: %s", tf, skipped_features)
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
    if not FINGERPRINT_FILE.exists():
        return True
    fp_mtime = FINGERPRINT_FILE.stat().st_mtime
    deps = [BUNDLE_FILE, CONFIG_FILE, SHORTLIST_FILE]
    for tf in TRAINING_AUDIT_TFS:
        try:
            deps.append(find_csv(tf))
        except Exception:
            pass
    for dep in deps:
        if dep.exists() and dep.stat().st_mtime > fp_mtime:
            return True
    return False


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
    if (not fingerprint_needs_rebuild()) and FINGERPRINT_FILE.exists():
        try:
            with open(FINGERPRINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            logging.info("[FINGERPRINT] loaded existing fingerprint")
            return data
        except Exception:
            pass
    data = build_training_fingerprint()
    with open(FINGERPRINT_FILE, "w", encoding="utf-8") as f:
        json.dump(to_jsonable(data), f, ensure_ascii=False, indent=2)
    append_jsonl_row(FINGERPRINT_HISTORY_FILE, data)
    logging.info("[FINGERPRINT] rebuilt and saved -> %s", FINGERPRINT_FILE)
    logging.info("[FINGERPRINT HISTORY] appended -> %s", FINGERPRINT_HISTORY_FILE)
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


def compute_recent_signal_monitor(window: int = LIVE_MONITOR_WINDOW) -> Optional[Dict[str, Any]]:
    if not SIGNAL_MONITOR_FILE.exists():
        return None
    try:
        df = pd.read_csv(SIGNAL_MONITOR_FILE)
    except Exception:
        return None
    if df.empty:
        return None
    df = df.tail(window).copy()
    rule_side = pd.to_numeric(df.get("rule_side"), errors="coerce").fillna(0)
    candidates = rule_side != 0
    ml_reached = pd.to_numeric(df.get("ml_prob"), errors="coerce").notna()
    ml_accept = bool_series(df["ml_accept"]) if "ml_accept" in df.columns else pd.Series(False, index=df.index)
    opened = df["opened"].astype(str).isin(["LONG", "SHORT"]) if "opened" in df.columns else pd.Series(False, index=df.index)
    cand_n = int(candidates.sum())
    long_n = int((rule_side == 1).sum())
    short_n = int((rule_side == -1).sum())
    return {
        "rows": int(len(df)),
        "rule_candidate_rate": float(cand_n / max(len(df), 1)),
        "rule_long_share": float(long_n / max(cand_n, 1)),
        "rule_short_share": float(short_n / max(cand_n, 1)),
        "ml_accept_rate_on_reached": float(ml_accept[ml_reached].mean()) if ml_reached.any() else 0.0,
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



def append_shadow_parity_row(event: Dict[str, Any]):
    row = {
        "t": event.get("t"),
        "rule_reason": event.get("rule_reason"),
        "rule_side": event.get("rule_side"),
        "ml_prob": event.get("ml_prob"),
        "ml_threshold": event.get("ml_threshold"),
        "ml_accept": event.get("ml_accept"),
        "opened": event.get("opened"),
        "closed_reason": event.get("closed_reason"),
        "position_before": event.get("position_before"),
        "position_after": event.get("position_after"),
        "bar_closed_now": event.get("bar_closed_now"),
        "valid_next_entry": event.get("valid_next_entry"),
    }
    append_csv_row(SHADOW_PARITY_FILE, row, SHADOW_PARITY_COLUMNS)


def append_rule_funnel_file(event: Dict[str, Any]):
    funnel = event.get("rule_funnel")
    if not funnel:
        return

    base = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "signal_bar_utc": event.get("t"),
        "rule_reason": event.get("rule_reason"),
        "rule_side": event.get("rule_side"),
        "opened": event.get("opened"),
        "closed_reason": event.get("closed_reason"),
        "position_before": event.get("position_before"),
        "position_after": event.get("position_after"),
        "bar_closed_now": event.get("bar_closed_now"),
        "valid_next_entry": event.get("valid_next_entry"),
    }

    detail_cols = [
        "setup", "trigger", "valid_signal_row", "family_pass", "trigger_pass",
        "one_h_soft_veto_pass", "final_filter_pass", "final_filter_name",
        "ml_reached", "ml_status", "ml_accept", "ml_prob", "ml_threshold",
        "close", "open", "range", "body_pct", "mom", "s1_mom", "vol_z_20", "hour",
    ]

    for key in ["long", "short"]:
        details = dict(funnel.get(key, {}) or {})
        row = dict(base)
        row["funnel_side"] = key.upper()
        for c in detail_cols:
            row[c] = details.get(c)
        row["details_json"] = _safe_json(details)
        append_csv_row(RULE_FUNNEL_FILE, row, RULE_FUNNEL_COLUMNS)


def append_sample_audit_file(event: Dict[str, Any]):
    sample = event.get("sample_features")
    if not sample:
        return
    row = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "signal_bar_utc": event.get("t"),
        "rule_reason": event.get("rule_reason"),
        "rule_side": event.get("rule_side"),
        "ml_prob": event.get("ml_prob"),
        "ml_threshold": event.get("ml_threshold"),
        "ml_accept": event.get("ml_accept"),
        "bar_closed_now": event.get("bar_closed_now"),
        "valid_next_entry": event.get("valid_next_entry"),
        "sample_features_json": _safe_json(sample),
    }
    append_csv_row(AUDIT_SAMPLE_FILE, row, SAMPLE_AUDIT_COLUMNS)


def append_root_debug_file(event: Dict[str, Any]):
    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "t": event.get("t"),
        "bar_closed_now": event.get("bar_closed_now"),
        "valid_next_entry": event.get("valid_next_entry"),
        "rule_side": event.get("rule_side"),
        "rule_reason": event.get("rule_reason"),
        "ml_prob": event.get("ml_prob"),
        "ml_threshold": event.get("ml_threshold"),
        "ml_accept": event.get("ml_accept"),
        "opened": event.get("opened"),
        "closed_reason": event.get("closed_reason"),
        "position_before": event.get("position_before"),
        "position_after": event.get("position_after"),
        "event_json": _safe_json(event),
    }
    append_csv_row(ROOT_DEBUG_FILE, row, ROOT_DEBUG_COLUMNS)


def append_audit_status(panel: pd.DataFrame, feature_diag: Dict[str, Any], signal_diag: Dict[str, Any], integrity_results: Dict[str, Dict[str, Any]], alignment_results: Dict[str, Dict[str, Any]]):
    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(panel)),
        "last_ts_open": pd.Timestamp(panel["ts_open"].max()).isoformat() if len(panel) else None,
        "feature_status": feature_diag.get("status"),
        "feature_warnings": " | ".join(feature_diag.get("warnings", [])[:8]),
        "signal_status": signal_diag.get("status"),
        "signal_warnings": " | ".join(signal_diag.get("warnings", [])[:8]),
        "feature_warn_n": int(len(feature_diag.get("warnings", []))),
        "signal_warn_n": int(len(signal_diag.get("warnings", []))),
        "missing_long_model_cols_in_panel": int(sum(c not in panel.columns for c in LONG_FEATURE_COLS)),
        "missing_short_model_cols_in_panel": int(sum(c not in panel.columns for c in SHORT_FEATURE_COLS)),
    }
    for tf in TRAINING_AUDIT_TFS:
        row[f"candle_{tf}_status"] = integrity_results.get(tf, {}).get("status")
        row[f"align_{tf}_status"] = alignment_results.get(tf, {}).get("status")
        row[f"candle_{tf}_bad_bounds"] = integrity_results.get(tf, {}).get("bad_bounds")
        row[f"align_{tf}_misaligned"] = alignment_results.get(tf, {}).get("misaligned")
    append_csv_row(AUDIT_STATUS_FILE, row, AUDIT_STATUS_COLUMNS)


def append_signal_monitor(event: Dict[str, Any]):
    row = {
        "t": event.get("t"),
        "bar_closed_now": event.get("bar_closed_now"),
        "valid_next_entry": event.get("valid_next_entry"),
        "rule_side": event.get("rule_side"),
        "rule_reason": event.get("rule_reason"),
        "ml_prob": event.get("ml_prob"),
        "ml_threshold": event.get("ml_threshold"),
        "ml_accept": event.get("ml_accept"),
        "opened": event.get("opened"),
        "closed_reason": event.get("closed_reason"),
        "position_before": event.get("position_before"),
        "position_after": event.get("position_after"),
        "event_json": _safe_json({k: v for k, v in event.items() if k not in {"sample_features", "rule_funnel"}}),
    }
    append_csv_row(SIGNAL_MONITOR_FILE, row, SIGNAL_MONITOR_COLUMNS)


def append_diagnostic_status(panel: pd.DataFrame):
    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(panel)),
        "last_ts_open": pd.Timestamp(panel["ts_open"].max()).isoformat() if len(panel) else None,
        "closed_rows": int(panel["bar_closed_now"].sum()) if "bar_closed_now" in panel.columns else 0,
        "valid_next_entry_rows": int(panel["valid_next_entry"].sum()) if "valid_next_entry" in panel.columns else 0,
        "missing_long_model_cols_in_panel": int(sum(c not in panel.columns for c in LONG_FEATURE_COLS)),
        "missing_short_model_cols_in_panel": int(sum(c not in panel.columns for c in SHORT_FEATURE_COLS)),
    }
    append_csv_row(DIAGNOSTIC_STATUS_FILE, row, DIAGNOSTIC_STATUS_COLUMNS)

def bootstrap_state_from_history(state: Dict[str, Any], decision_source: pd.DataFrame) -> Dict[str, Any]:
    if decision_source.empty:
        state["initialized"] = True
        return state
    state["last_processed_bar"] = pd.Timestamp(decision_source.iloc[-1]["ts_open"]).isoformat()
    state["position"] = None
    state["initialized"] = True
    logging.info("[BOOTSTRAP DONE] started FLAT | last_processed_bar=%s", state["last_processed_bar"])
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
        logging.info("[INFO] live panel empty")
        return state

    append_diagnostic_status(panel)

    decision_source = panel[(panel["bar_closed_now"]) & (panel["valid_next_entry"])].copy().sort_values("ts_open").reset_index(drop=True)

    if AUDIT_MODE:
        log_bar_mode(panel, decision_source, now_utc)
        log_candle_integrity(integrity_results)
        log_alignment_checks(alignment_results)
        feature_diag = compare_live_features_to_training(panel, LIVE_MONITOR_WINDOW)
        signal_metrics = compute_recent_signal_monitor(LIVE_MONITOR_WINDOW)
        signal_diag = compare_signal_monitor_to_fingerprint(signal_metrics)
        append_audit_status(panel, feature_diag, signal_diag, integrity_results, alignment_results)
        logging.info(
            "[TRAINING DRIFT CHECK] feature_status=%s | feature_warn_n=%d | signal_status=%s | signal_warn_n=%d",
            feature_diag.get("status"),
            len(feature_diag.get("warnings", [])),
            signal_diag.get("status"),
            len(signal_diag.get("warnings", [])),
        )
        if feature_diag.get("warnings"):
            logging.warning("[FEATURE WARNINGS] %s", " | ".join(feature_diag["warnings"][:6]))
        if signal_diag.get("warnings"):
            logging.warning("[SIGNAL WARNINGS] %s", " | ".join(signal_diag["warnings"][:6]))
        logging.info("[FEATURE DRIFT] window=%d | %s", DRIFT_WINDOW, compute_feature_drift(panel, DRIFT_WINDOW))

    if decision_source.empty:
        logging.info("[INFO] no closed 15m signal bars with next entry")
        return state

    logging.info(
        "[PANEL] rows=%d closed=%d valid_next=%d last_closed_signal=%s",
        len(panel),
        int(panel["bar_closed_now"].sum()),
        int(panel["valid_next_entry"].sum()),
        pd.Timestamp(decision_source.iloc[-1]["ts_open"]).isoformat(),
    )

    if not state.get("initialized", False):
        return bootstrap_state_from_history(state, decision_source)

    last_processed = pd.Timestamp(state["last_processed_bar"]) if state.get("last_processed_bar") else None
    new_rows = decision_source if last_processed is None else decision_source[decision_source["ts_open"] > last_processed].copy()
    new_rows = new_rows.sort_values("ts_open").reset_index(drop=True)

    live_mid = fetch_live_price()
    latest_bar = float(decision_source.iloc[-1]["close"])
    if live_mid is None or not np.isfinite(live_mid):
        live_mid = latest_bar
    bid, ask = current_bid_ask_from_mid(float(live_mid))

    if new_rows.empty:
        pos = load_position(state)
        pos_side = pos.side if pos else 0
        mtm = bid if pos_side == +1 else ask if pos_side == -1 else float(live_mid)
        logging.info(
            "[NO NEW BAR] bid=%.2f ask=%.2f mtm=%.2f (bar=%.2f) latest_closed=%.2f pos=%s",
            bid, ask, mtm, latest_bar, latest_bar, position_txt(pos_side),
        )
        return state

    if needs_silent_catchup(last_processed, new_rows):
        for _, row in new_rows.iterrows():
            state, event = process_one_signal_bar(row, state, send_alerts=False)
            append_signal_monitor(event)
            if AUDIT_MODE:
                append_shadow_parity_row(event)
                append_rule_funnel_file(event)
                append_sample_audit_file(event)
                append_root_debug_file(event)
        logging.info("[CATCHUP MODE] processed %d 15m bars with alerts disabled", len(new_rows))
        return state

    last_event = None
    for _, row in new_rows.iterrows():
        state, last_event = process_one_signal_bar(row, state, send_alerts=True)
        append_signal_monitor(last_event)
        if AUDIT_MODE:
            append_shadow_parity_row(last_event)
            append_rule_funnel_file(last_event)
            append_sample_audit_file(last_event)
            append_root_debug_file(last_event)

    pos = load_position(state)
    pos_side = pos.side if pos else 0
    mtm = bid if pos_side == +1 else ask if pos_side == -1 else float(live_mid)
    logging.info(
        "[LIVE] t=%s bid=%.2f ask=%.2f mtm=%.2f (bar=%.2f) rule=%s side=%s ml_p=%s thr=%s accept=%s opened=%s closed=%s pos=%s",
        last_event.get("t"),
        bid,
        ask,
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
    if AUDIT_MODE and last_event is not None:
        logging.info(
            "[ROOT DEBUG FILE] event written | file=%s | rule=%s | side=%s | ml_accept=%s",
            ROOT_DEBUG_FILE,
            last_event.get("rule_reason"),
            last_event.get("rule_side"),
            last_event.get("ml_accept"),
        )
        logging.info("[RULE FUNNEL FILE] event written | file=%s", RULE_FUNNEL_FILE)
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
        "final_trades_3626": final_trades == EXPECTED_FINAL_TRADES,
        "long_threshold_0400": abs(long_thr - EXPECTED_LONG_THRESHOLD) < 1e-9,
        "short_threshold_0390": abs(short_thr - EXPECTED_SHORT_THRESHOLD) < 1e-9,
        "long_source_nonzero": long_candidates > 0,
        "loaded_source_matches_summary": len(V22_LONG_SOURCE_MAP) == long_candidates,
    }
    logging.info("[EXPORT ENGINE CHECK] dir=%s", V22_ENGINE_EXPORT_DIR)
    logging.info("[EXPORT ENGINE CHECK] status=%s final_trades=%s long_thr=%.3f short_thr=%.3f long_source_candidates=%s loaded=%s",
                 status, final_trades, long_thr, short_thr, long_candidates, len(V22_LONG_SOURCE_MAP))
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"V22 live engine export check failed: {failed}")
    logging.info("[EXPORT ENGINE CHECK] PASS")


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

    logging.info("🟢 Live ETHUSDT 15m V22 NEW EXPORT ENGINE up — V22_LONG + SHORT_NO_FILTER + mandatory separate ML")
    logging.info("[base_dir] %s", BASE_DIR)
    logging.info("[live_audit_dir] %s", LIVE_AUDIT_DIR)
    logging.info("[bundle] %s", BUNDLE_FILE)
    logging.info("[config] %s", CONFIG_FILE)
    logging.info("[shortlist] %s", SHORTLIST_FILE)
    logging.info("[v22_engine_export_dir] %s", V22_ENGINE_EXPORT_DIR)
    logging.info("[v22_engine_decision_config] %s", V22_ENGINE_DECISION_CONFIG_FILE)
    logging.info("[v22_engine_long_engine] %s", V22_ENGINE_LONG_ENGINE_FILE)
    logging.info("[v22_engine_parity_summary] %s", V22_ENGINE_PARITY_SUMMARY_FILE)
    logging.info("[v22_long_source] %s", V22_LONG_SOURCE_FILE)
    logging.info("[long_threshold] %.3f | [short_threshold] %.3f", LONG_THRESHOLD, SHORT_THRESHOLD)
    if abs(LONG_THRESHOLD - EXPECTED_LONG_THRESHOLD) > 1e-9 or abs(SHORT_THRESHOLD - EXPECTED_SHORT_THRESHOLD) > 1e-9:
        raise RuntimeError(f"Threshold mismatch: long={LONG_THRESHOLD} short={SHORT_THRESHOLD}; expected long={EXPECTED_LONG_THRESHOLD} short={EXPECTED_SHORT_THRESHOLD}")
    logging.info("[long_features] %d | [short_features] %d", len(LONG_FEATURE_COLS), len(SHORT_FEATURE_COLS))
    logging.info("[entry_mode] NEXT 15m open")
    logging.info("[execution] 15m only; global one-position; LONG_FIRST; exact SHORT q50/q80; V22 LONG from new export engine + causal future fallback; startup audit uses 15m/1h/4h/1d only; no 1m/5m execution; no pending/retest; no flip logic")
    logging.info("[audit_mode] %s", AUDIT_MODE)
    logging.info("[fingerprint_file] %s", FINGERPRINT_FILE)
    logging.info("[fingerprint_history_file] %s", FINGERPRINT_HISTORY_FILE)
    logging.info("[sample_audit_file] %s", AUDIT_SAMPLE_FILE)
    logging.info("[root_debug_file] %s", ROOT_DEBUG_FILE)
    logging.info("[rule_funnel_file] %s", RULE_FUNNEL_FILE)
    logging.info("[shadow_parity_file] %s", SHADOW_PARITY_FILE)
    logging.info("[audit_status_file] %s", AUDIT_STATUS_FILE)
    logging.info("[signal_monitor_file] %s", SIGNAL_MONITOR_FILE)
    logging.info("[trade_log_file] %s", TRADE_LOG_FILE)

    run_startup_export_engine_check()

    if AUDIT_MODE:
        run_local_feature_audit()
        if AUDIT_ONLY_ON_STARTUP:
            logging.info("[AUDIT MODE] startup feature audit complete")
        TRAINING_FINGERPRINT = load_or_build_training_fingerprint()
        log_training_fingerprint_summary(TRAINING_FINGERPRINT)
        if RUN_STARTUP_FULL_PARITY_REPLAY:
            run_startup_full_parity_replay()

    state = default_runtime_state()
    state = run_once(state)

    while True:
        try:
            time.sleep(seconds_until_next_15m(datetime.now(timezone.utc)) + 1)
            state = run_once(state)
        except KeyboardInterrupt:
            logging.info("[STOPPED]")
            break
        except Exception as e:
            logging.error("[LOOP ERROR] %s", e, exc_info=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
