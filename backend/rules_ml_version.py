#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETHUSDT LAST 2 YEARS — 15M VERSION B NEW RULES + MANDATORY ML — SOFT VETO V1 + LONG FINAL Q60 + SHORT TRIGGER Q50 + LONG CLOSE_POS Q50 + LONG ADX Q55
======================================================================================

Final structure implemented exactly:
1) LONG is kept as the current locked high-quality setup.
2) SHORT runs Version B only:
   - SHORT_NO_FILTER    = no extra short filter
3) ML is mandatory after rules for every final candidate.
4) LONG and SHORT models are trained separately.
5) Rule selection and ML threshold/model selection use train + validation only.
6) Test remains audit/evaluation only.
7) Entry is NEXT 15m open. No 5m execution.
8) Console final report prints Version B only:
   - Version B — Volume: SHORT_NO_FILTER + ML
9) Confirmation Mode: stronger ML confirmation, live artifacts/files are saved.
10) SHORT exit fixed to FAST_TRAIL_0.50_0.30 only.
11) Adds safety/overfitting/stability audits and Fast Mode benchmark comparison.
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
pd.set_option("display.width", 280)
pd.set_option("display.max_columns", 240)
pd.set_option("display.max_rows", 240)

# ==============================================================================
# CONFIG
# ==============================================================================

BASE_DIR = Path("/Users/omarhassan/Desktop/project/Eth/test backup")
SHORTLIST_FILE = BASE_DIR / "eth_feature_shortlist_outputs" / "ethusdt_feature_shortlist_best3_global.csv"
SYMBOL = "ETHUSDT"

START_DATE = pd.Timestamp("2024-04-01 00:00:00")
END_DATE = pd.Timestamp("2026-03-31 23:59:59")

TRAIN_RATIO = 0.60
VAL_RATIO = 0.20
ROUND_TRIP_COST = 0.001200

BASE_TF = "15m"
HTF_TFS = ["1h", "4h", "1d"]
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
EXPECTED_NEXT_MINUTES = TF_MINUTES[BASE_TF]

NO_OVERLAP = True
SAME_BAR_POLICY = "worst"
USE_1H_SOFT_VETO = True
SAVE_ML_ARTIFACTS = True  # FINAL EXPORT: save model artifacts/config files using global no-overlap threshold selection.

# Keep LONG exactly on current locked result from the last good run.
LONG_SETUP_FAMILY = "BTC_ETHBTC_CONTEXT"
LONG_SETUP_NAME = "fam__BTC_ETHBTC_CONTEXT"
LONG_TRIGGER = "adx_breakout"
LONG_FINAL_FILTER_KIND = "di60_and_session_not_late"
LONG_EXIT_NAME = "trail0.60_0.30_sl1.30_tp1.50_hold24"
LONG_EXIT_SL_ATR = 1.30
LONG_EXIT_TP_ATR = 1.50
LONG_EXIT_TRAIL_START_ATR = 0.60
LONG_EXIT_TRAIL_DIST_ATR = 0.30
LONG_EXIT_MAX_HOLD_BARS = 24

# Keep SHORT setup/trigger fixed, only expand the extra direct filter.
SHORT_LOCKED_SETUP_FAMILY = "VOLATILITY"
SHORT_LOCKED_SETUP_NAME = "fam__VOLATILITY"
SHORT_LOCKED_TRIGGER = "momentum_break"
SHORT_EXIT_NAME = "trail0.50_0.30_sl1.15_tp2.20_hold8"
SHORT_EXIT_SL_ATR = 1.15
SHORT_EXIT_TP_ATR = 2.20
SHORT_EXIT_TRAIL_START_ATR = 0.50
SHORT_EXIT_TRAIL_DIST_ATR = 0.30
SHORT_EXIT_MAX_HOLD_BARS = 8
SHORT_USE_SHORTLIST_FEATURE_FIELD_FIRST = True
SHORT_EMA_SLOPE_FILTER_COL_REQUIRED = "ema20_slope_10"

SHORT_EXPANSION_VARIANTS = ["SHORT_NO_FILTER"]

# FINAL CLEAN EXECUTION POLICY
# No forced target TPD. The model is judged on natural train+validation performance.
# After ML accepts candidate trades, a single chronological global no-overlap engine is applied
# across LONG and SHORT together: one open ETHUSDT position at a time.
USE_FORCED_TARGET_TPD = False
GLOBAL_NO_OVERLAP_AFTER_ML = True
GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY = "LONG_FIRST"  # deterministic tie-break when both sides accepted on same entry bar.
NO_FLIP_ON_OPPOSITE_SIGNAL = True  # ignore opposite-side signals while a position is open.

ML_ARTIFACT_DIR = BASE_DIR / "model files"
ML_BUNDLE_FILE = "ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib"
ML_CONFIG_FILE = "ethusdt_15m_short_expansion_mandatory_ml_config.json"

# Fast Mode benchmark from the last accepted run, used only for final console comparison.
FAST_MODE_BENCHMARK = {
    "label": "FAST_MODE_ACCEPTED_SHORT_TRAIL_0.50_0.30",
    "trades": 1910,
    "tpd": 2.616,
    "wr": 0.7377,
    "net": 3.60896,
    "pf": 1.982,
    "dd": 0.06530,
    "avgNet": 0.001890,
}

ML = {
    "N_TRIALS": 60,
    "N_WF_FOLDS": 8,
    "RECENT_TRAIN_MAX": 1600,
    "CALIB_FIT_PCT": 0.25,
    "CALIB_MIN_ROWS": 30,
    "EMBARGO_BARS": 32,
    "MAX_SNAPSHOT_FEATURES": 120,
    "THR_MIN": 0.05,
    "THR_MAX": 0.85,
    "THR_STEPS": 81,
    "MIN_TAKEN_LONG": 8,
    "MIN_TAKEN_SHORT": 25,
    "LIGHT_VETO_MIN_COVERAGE_LONG": 0.70,
    "LIGHT_VETO_TARGET_COVERAGE_LONG": 0.85,
    "LIGHT_VETO_MIN_COVERAGE_SHORT": 0.75,
    "LIGHT_VETO_TARGET_COVERAGE_SHORT": 0.88,
    "MAX_VETO_RATE_LONG": 0.30,
    "MAX_VETO_RATE_SHORT": 0.25,
    "LOCAL_DD_CAP_FRAC": 1.05,
    "LOCAL_DD_CAP_MIN": 0.025,
    "LABEL_MIN_NET_LONG": 0.0015,
    "LABEL_MIN_NET_SHORT": 0.0012,
    "LABEL_ATR_MULT_LONG": 0.25,
    "LABEL_ATR_MULT_SHORT": 0.20,
    "LABEL_BREAK_EXTRA": 0.0005,
    "SCORE_NET_W": 60.0,
    "SCORE_AVGNET_W": 2600.0,
    "SCORE_PF_W": 6.0,
    "SCORE_WR_W": 2.0,
    "SCORE_DD_W": 45.0,
    "SCORE_TRADE_W": 0.04,
    "SCORE_COVERAGE_W": 14.0,
    "SCORE_VETO_BADNESS_W": 18.0,
    "SCORE_EXCESS_VETO_PENALTY_W": 30.0,
}

CALIBRATION_METHODS = ["sigmoid", "none"]

# ==============================================================================
# PRINT HELPERS
# ==============================================================================

def hr(ch: str = "=", n: int = 118) -> None:
    print(ch * n)


def section(title: str) -> None:
    print(); hr("="); print(title); hr("=")


def subsection(title: str) -> None:
    print(); hr("-"); print(title); hr("-")


def safe_div(a: float, b: float, default: float = np.nan) -> float:
    try:
        if b == 0 or pd.isna(b):
            return default
        return a / b
    except Exception:
        return default


def fmt_pf(x: float) -> str:
    if x is None or pd.isna(x):
        return "nan"
    if np.isinf(x):
        return "inf"
    return f"{x:.3f}"


def die(msg: str) -> None:
    print(); hr("!"); print("STOPPED"); print(msg); hr("!")
    raise SystemExit(1)

# ==============================================================================
# DATA LOADING
# ==============================================================================

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
    exact = [p for p in hits if p.name.startswith(f"{SYMBOL}_{tf}_BINANCE_")]
    return exact[-1] if exact else hits[-1]


def load_tf(tf: str) -> pd.DataFrame:
    path = find_csv(tf)
    print(f"Loading {tf}: {path}")
    df = pd.read_csv(path, encoding="latin1", low_memory=False)
    df = add_time_columns(df, tf)
    for c in ["open", "high", "low", "close"]:
        if c not in df.columns:
            raise RuntimeError(f"{tf}: missing required OHLC column {c}")
        df[c] = pd.to_numeric(df[c], errors="coerce")
    print(f"{tf}: rows={len(df):,} columns={len(df.columns):,} range={df['ts_open'].min()} -> {df['ts_open'].max()}")
    return df


def attach_htf(base: pd.DataFrame, htf: pd.DataFrame, tf: str) -> pd.DataFrame:
    base = base.sort_values("ts_close").reset_index(drop=True).copy()
    htf = htf.sort_values("ts_close").reset_index(drop=True).copy()
    reserved = {"ts_open", "ts_close", "entry_time_next", "entry_ts_next", "entry_open_next", "next_index", "valid_next_entry", "entry_gap_minutes"}
    rename = {c: f"{tf}__{c}" for c in htf.columns if c not in reserved}
    h = htf.rename(columns=rename)
    keep = ["ts_close"] + list(rename.values())
    h = h[keep].rename(columns={"ts_close": f"{tf}__ts_close"})
    out = pd.merge_asof(base, h.sort_values(f"{tf}__ts_close"), left_on="ts_close", right_on=f"{tf}__ts_close", direction="backward", allow_exact_matches=True)
    alias_count = 0
    pref = f"{tf}__"
    for c in list(out.columns):
        if c.startswith(pref):
            raw = c[len(pref):]
            alias = f"{raw}_{tf}"
            if alias not in out.columns:
                out[alias] = out[c]
                alias_count += 1
    future = int((out[f"{tf}__ts_close"] > out["ts_close"]).sum()) if f"{tf}__ts_close" in out.columns else 0
    print(f"{tf}: attached_columns={len(rename):,} alias_columns={alias_count:,} future_rows={future}")
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
    created = []
    if "range" not in panel.columns:
        panel["range"] = h - l; created.append("range")
    if "body" not in panel.columns:
        panel["body"] = (c - o).abs(); created.append("body")
    if "body_pct" not in panel.columns:
        panel["body_pct"] = ((c - o).abs() / rng).clip(lower=0, upper=5); created.append("body_pct")
    if "upper_wick_pct" not in panel.columns:
        panel["upper_wick_pct"] = ((h - np.maximum(o, c)) / rng).clip(lower=0, upper=5); created.append("upper_wick_pct")
    if "lower_wick_pct" not in panel.columns:
        panel["lower_wick_pct"] = ((np.minimum(o, c) - l) / rng).clip(lower=0, upper=5); created.append("lower_wick_pct")
    if "close_pos" not in panel.columns:
        panel["close_pos"] = ((c - l) / rng).clip(lower=0, upper=1); created.append("close_pos")
    if "candle_direction" not in panel.columns:
        panel["candle_direction"] = np.where(c > o, 1, np.where(c < o, -1, 0)); created.append("candle_direction")
    print(f"basic_ohlc_helper_columns_created_in_memory={created if created else 'NONE'}")
    return panel


def rebuild_execution_after_filter(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.sort_values("ts_open").reset_index(drop=True).copy()
    for col in ["entry_time_next", "entry_ts_next", "entry_open_next", "next_index", "valid_next_entry", "entry_gap_minutes"]:
        if col in panel.columns:
            panel = panel.drop(columns=[col])
    panel["entry_time_next"] = panel["ts_open"].shift(-1)
    panel["entry_ts_next"] = panel["entry_time_next"]
    panel["entry_open_next"] = pd.to_numeric(panel["open"].shift(-1), errors="coerce")
    panel["next_index"] = np.arange(len(panel), dtype="float64") + 1.0
    panel.loc[panel.index[-1], ["entry_time_next", "entry_ts_next", "entry_open_next", "next_index"]] = [pd.NaT, pd.NaT, np.nan, np.nan]
    gap = (panel["entry_time_next"] - panel["ts_open"]).dt.total_seconds() / 60.0
    panel["entry_gap_minutes"] = gap
    panel["valid_next_entry"] = panel["entry_open_next"].notna() & panel["next_index"].notna() & np.isclose(panel["entry_gap_minutes"].fillna(-9999.0), EXPECTED_NEXT_MINUTES)
    bad_gap = int((panel["entry_gap_minutes"].notna() & ~np.isclose(panel["entry_gap_minutes"].fillna(-9999.0), EXPECTED_NEXT_MINUTES)).sum())
    print("Execution columns rebuilt AFTER 2-year filter and reset_index.")
    print(f"valid_next_entry_rows={int(panel['valid_next_entry'].sum()):,}/{len(panel):,}")
    print(f"blocked_signal_rows_no_valid_next_open={int((~panel['valid_next_entry']).sum()):,}")
    print(f"bad_15m_entry_gaps={bad_gap:,}")
    print(f"last_row_blocked_as_signal={not bool(panel['valid_next_entry'].iloc[-1])}")
    return panel


def load_shared_panel() -> pd.DataFrame:
    section("LOAD DATA — SHARED 15M SIGNAL PANEL + CLOSED HTF CONTEXT")
    print(f"BASE_DIR={BASE_DIR}")
    print("HTF attach = merge_asof backward on candle close time.")
    print("15m execution = entry at NEXT 15m open.")
    panel = load_tf("15m")
    for tf in HTF_TFS:
        panel = attach_htf(panel, load_tf(tf), tf)
    section("PANEL BEFORE LAST 2 YEARS FILTER")
    print(f"final_rows_before_last2y_filter={len(panel):,}")
    print(f"final_columns={len(panel.columns):,}")
    print(f"date_range_before_filter={panel['ts_open'].min()} -> {panel['ts_open'].max()}")
    future = 0
    for tf in HTF_TFS:
        c = f"{tf}__ts_close"
        if c in panel.columns:
            future += int((panel[c] > panel["ts_close"]).sum())
    print(f"future_attach_rows_should_be_0={future}")
    if future != 0:
        raise RuntimeError(f"HTF lookahead detected: total_future_attach_rows={future}")
    section("LAST 2 YEARS FILTER + EXECUTION REBUILD")
    before = len(panel)
    panel = panel[(panel["ts_open"] >= START_DATE) & (panel["ts_open"] <= END_DATE)].copy()
    panel = panel.sort_values("ts_open").reset_index(drop=True)
    print(f"LAST_TWO_YEAR_START={START_DATE}")
    print(f"LAST_TWO_YEAR_END={END_DATE}")
    print(f"rows_before_filter={before:,}")
    print(f"rows_after_filter={len(panel):,}")
    print(f"last2y_date_range={panel['ts_open'].min()} -> {panel['ts_open'].max()}")
    panel = ensure_basic_ohlc_helpers(panel)
    panel = rebuild_execution_after_filter(panel)
    return panel

# ==============================================================================
# SPLITS + COLUMN HELPERS
# ==============================================================================

@dataclass(frozen=True)
class SplitDef:
    name: str
    start: int
    end: int


def make_splits(panel: pd.DataFrame) -> Dict[str, SplitDef]:
    n = len(panel)
    n_train = int(n * TRAIN_RATIO)
    n_val = int(n * VAL_RATIO)
    splits = {
        "train": SplitDef("train", 0, n_train),
        "validation": SplitDef("validation", n_train, n_train + n_val),
        "test": SplitDef("test", n_train + n_val, n),
    }
    section("SPLIT — LAST 2 YEARS 60/20/20")
    for name, sp in splits.items():
        part = panel.iloc[sp.start:sp.end]
        if part.empty:
            raise RuntimeError(f"Empty split: {name}")
        print(f"{name:<10} rows={len(part):>8,} range={part['ts_open'].min()} -> {part['ts_open'].max()}")
    print("Selection uses train + validation only. Test is audit only.")
    return splits


def period_days_from_panel(panel: pd.DataFrame, start: int, end: int) -> int:
    part = panel.iloc[start:end]
    if part.empty:
        return 0
    d0 = pd.Timestamp(part["ts_open"].min()).floor("D")
    d1 = pd.Timestamp(part["ts_open"].max()).floor("D")
    return int((d1 - d0).days + 1)


def build_period_days(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> Dict[str, int]:
    out = {}
    for name, sp in splits.items():
        out[name] = period_days_from_panel(panel, sp.start, sp.end)
    out["full_2y"] = period_days_from_panel(panel, 0, len(panel))
    return out


def first_col(df: pd.DataFrame, names: List[str], required: bool = False, label: str = "") -> Optional[str]:
    for c in names:
        if c in df.columns:
            return c
    low = {str(c).lower(): c for c in df.columns}
    for c in names:
        if c.lower() in low:
            return low[c.lower()]
    if required:
        raise RuntimeError(f"Missing required column {label}: {names}")
    return None


def require_cols(df: pd.DataFrame, cols: Iterable[str], label: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        die(f"Missing required columns for {label}: {missing}")


def num(df: pd.DataFrame, col: Optional[str], default: float = np.nan) -> pd.Series:
    if col is None or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def bool_col(df: pd.DataFrame, col: Optional[str]) -> pd.Series:
    if col is None or col not in df.columns:
        return pd.Series(False, index=df.index)
    s = df[col]
    x = pd.to_numeric(s, errors="coerce")
    if x.notna().any():
        return x.fillna(0) != 0
    return s.astype("string").str.lower().isin(["true", "1", "yes", "y"])


def qtrain(df: pd.DataFrame, splits: Dict[str, SplitDef], col: Optional[str], q: float, required: bool = False) -> float:
    if col is None or col not in df.columns:
        if required:
            die(f"Cannot compute train quantile; missing column: {col}")
        return np.nan
    sp = splits["train"]
    s = pd.to_numeric(df.iloc[sp.start:sp.end][col], errors="coerce").dropna()
    if not len(s):
        if required:
            die(f"Cannot compute train quantile for empty column: {col}")
        return np.nan
    return float(s.quantile(q))


def mask_rate(name: str, mask: pd.Series) -> None:
    print(f"{name:<105} pass={int(mask.sum()):,}/{len(mask):,} rate={mask.mean()*100:.3f}%")

# ==============================================================================
# SHORTLIST SPECS
# ==============================================================================

LEAKY_PATTERNS = ("future", "target", "label", "pnl", "profit", "mfe", "mae", "exit", "tp_hit", "sl_hit", "outcome", "ret_fwd", "forward")


def is_leaky_col(name: str) -> bool:
    x = str(name).lower()
    return any(p in x for p in LEAKY_PATTERNS)


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


def load_shortlist_raw() -> pd.DataFrame:
    section("LOAD SAVED FEATURE SHORTLIST")
    print(f"SHORTLIST_FILE={SHORTLIST_FILE}")
    if not SHORTLIST_FILE.exists():
        die("Saved shortlist CSV not found. No fallback is allowed.")
    df = pd.read_csv(SHORTLIST_FILE, encoding="latin1", low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    print(f"feature_specs_raw={len(df):,}")
    print(f"shortlist_columns={list(df.columns)}")
    return df


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
    section(f"{label} SHORTLIST AUDIT AGAINST DATA")
    specs: List[FeatureSpec] = []
    missing = 0; unparsed = 0; blocked = 0
    for i, row in shortlist.iterrows():
        side = norm_side(row.get("side", ""))
        fam = norm_family(row.get("family", ""))
        feature = clean_token(row.get("feature", ""))
        col_raw = clean_token(row.get("column", ""))
        joined = f"{feature} {col_raw}".lower()
        if any(w in joined for w in LEAKY_PATTERNS):
            blocked += 1; continue
        op = parse_op(row)
        if op is None:
            unparsed += 1; continue
        try:
            thr = float(row.get("threshold"))
        except Exception:
            unparsed += 1; continue
        src = resolve_col(panel, row, feature_first=feature_first)
        if src is None:
            missing += 1; continue
        specs.append(FeatureSpec(int(i), side, fam, feature, col_raw, clean_token(row.get("timeframe", "")), op, thr, src))
    print(f"input_specs={len(shortlist):,}")
    print(f"kept_specs={len(specs):,}")
    print(f"missing_or_unparsed_specs={unparsed:,}")
    print(f"missing_feature_columns={missing:,}")
    print(f"blocked_leaky_specs={blocked:,}")
    if specs:
        vc = pd.Series([(s.side, s.family) for s in specs]).value_counts().sort_index()
        print("\nspec_counts_by_side_family:")
        for (side, fam), cnt in vc.items():
            print(f"  {side:<5} {fam:<28} {cnt}")
    return specs


def apply_spec(panel: pd.DataFrame, sp: FeatureSpec) -> pd.Series:
    s = num(panel, sp.source_column)
    return ((s <= sp.threshold) if sp.op == "<=" else (s >= sp.threshold)).fillna(False)


def build_family_mask(panel: pd.DataFrame, specs: List[FeatureSpec], side: str, family: str, label: str) -> pd.Series:
    section(f"{label} — BUILD LOCKED SETUP MASK")
    side = side.upper(); family = family.upper().replace("FAM__", "")
    fs = [s for s in specs if s.side == side and s.family == family]
    print(f"locked_side={side}")
    print(f"locked_setup=fam__{family}")
    print(f"family_specs_found={len(fs):,}")
    if not fs:
        die(f"No usable saved shortlist specs found for side={side}, family={family}.")
    mask = pd.Series(False, index=panel.index)
    for sp in fs:
        m = apply_spec(panel, sp)
        mask |= m
        print(f"  row={sp.row_i:<4} {sp.source_column:<35} {sp.op} {sp.threshold:.8g} pass={int(m.sum()):,} source_feature={sp.feature} source_column={sp.column_raw}")
    print(f"fam__{family}_mask_pass={int(mask.sum()):,}/{len(panel):,} rate={mask.mean() * 100:.3f}%")
    print("fallback_note=NO_FALLBACK_USED")
    return mask

# ==============================================================================
# BACKTEST
# ==============================================================================

@dataclass(frozen=True)
class ExitConfig:
    name: str
    mode: str
    sl_atr: float
    tp_atr: float
    hold_bars: int
    trail_trigger_atr: Optional[float] = None
    trail_dist_atr: Optional[float] = None


def get_atr_abs(panel: pd.DataFrame, idx: int, entry: float) -> float:
    atr_col = first_col(panel, ["atr_14", "atr"])
    atrp_col = first_col(panel, ["atrp_14", "atrp"])
    atr = pd.to_numeric(panel.at[idx, atr_col], errors="coerce") if atr_col else np.nan
    if pd.isna(atr) or atr <= 0:
        atrp = pd.to_numeric(panel.at[idx, atrp_col], errors="coerce") if atrp_col else np.nan
        if pd.notna(atrp) and atrp > 0:
            atr = entry * float(atrp)
    if pd.isna(atr) or atr <= 0:
        hi = pd.to_numeric(panel.at[idx, "high"], errors="coerce")
        lo = pd.to_numeric(panel.at[idx, "low"], errors="coerce")
        atr = max(float(hi - lo), entry * 0.002) if pd.notna(hi) and pd.notna(lo) else entry * 0.002
    return float(atr)


def session_name(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia_00_07"
    if 7 <= hour < 13:
        return "london_07_13"
    if 13 <= hour < 22:
        return "us_13_22"
    return "late_22_24"


def simulate_long(panel: pd.DataFrame, signal_mask: pd.Series, cfg: ExitConfig, start: int = 0, end: Optional[int] = None) -> pd.DataFrame:
    if end is None:
        end = len(panel)
    signal = signal_mask.fillna(False).to_numpy(bool)
    valid_next = panel["valid_next_entry"].fillna(False).to_numpy(bool)
    opens = pd.to_numeric(panel["open"], errors="coerce").to_numpy(float)
    highs = pd.to_numeric(panel["high"], errors="coerce").to_numpy(float)
    lows = pd.to_numeric(panel["low"], errors="coerce").to_numpy(float)
    closes = pd.to_numeric(panel["close"], errors="coerce").to_numpy(float)
    rows: List[Dict[str, Any]] = []
    i = start
    last_signal_i = min(end - 2, len(panel) - 2)
    while i <= last_signal_i:
        if not signal[i] or not valid_next[i]:
            i += 1; continue
        ent_i = i + 1
        if ent_i >= end:
            break
        entry = float(opens[ent_i])
        if not np.isfinite(entry) or entry <= 0:
            i += 1; continue
        atr = get_atr_abs(panel, i, entry)
        sl_price = entry - cfg.sl_atr * atr
        tp_price = entry + cfg.tp_atr * atr
        best_high = entry; worst_low = entry
        trail_active = False; trail_stop = -np.inf
        exit_i = min(end - 1, ent_i + cfg.hold_bars - 1)
        exit_price = float(closes[exit_i]); reason = "TO"
        for j in range(ent_i, min(end, ent_i + cfg.hold_bars)):
            hi, lo, cl = highs[j], lows[j], closes[j]
            if not np.isfinite(hi) or not np.isfinite(lo) or not np.isfinite(cl):
                continue
            best_high = max(best_high, hi); worst_low = min(worst_low, lo)
            sl_hit = lo <= sl_price; tp_hit = hi >= tp_price
            if sl_hit and tp_hit:
                exit_i, exit_price, reason = j, sl_price, "SL"; break
            if sl_hit:
                exit_i, exit_price, reason = j, sl_price, "SL"; break
            if tp_hit and cfg.mode == "fixed":
                exit_i, exit_price, reason = j, tp_price, "TP"; break
            if cfg.mode == "trail" and cfg.trail_trigger_atr is not None:
                if hi >= entry + cfg.trail_trigger_atr * atr:
                    trail_active = True
                    trail_stop = max(trail_stop, best_high - float(cfg.trail_dist_atr) * atr)
            if trail_active:
                trail_stop = max(trail_stop, best_high - float(cfg.trail_dist_atr) * atr)
                if lo <= trail_stop:
                    exit_i, exit_price, reason = j, trail_stop, "TR"; break
            exit_i, exit_price, reason = j, cl, "TO"
        gross = exit_price / entry - 1.0
        net = gross - ROUND_TRIP_COST
        mfe = best_high / entry - 1.0
        mae = entry / worst_low - 1.0 if worst_low > 0 else np.nan
        et = pd.Timestamp(panel.at[ent_i, "ts_open"])
        rows.append({"engine": "LONG", "side": "LONG", "signal_i": i, "entry_i": ent_i, "exit_i": exit_i, "signal_time": panel.at[i, "ts_open"], "entry_time": et, "exit_time": panel.at[exit_i, "ts_open"], "entry": entry, "exit": exit_price, "gross": gross, "net": net, "cost": ROUND_TRIP_COST, "reason": reason, "bars": exit_i - ent_i + 1, "MFE": mfe, "MAE": mae, "MFE_MAE": safe_div(mfe, mae), "hour": int(et.hour), "dow": et.day_name(), "month": et.strftime("%Y-%m"), "quarter": f"{et.year}Q{et.quarter}", "year": str(et.year), "session": session_name(int(et.hour))})
        i = exit_i + 1 if NO_OVERLAP else i + 1
    return pd.DataFrame(rows)


def simulate_short(panel: pd.DataFrame, signal_mask: pd.Series, cfg: ExitConfig, start: int = 0, end: Optional[int] = None) -> pd.DataFrame:
    if end is None:
        end = len(panel)
    signal = signal_mask.fillna(False).to_numpy(bool)
    valid_next = panel["valid_next_entry"].fillna(False).to_numpy(bool)
    opens = pd.to_numeric(panel["open"], errors="coerce").to_numpy(float)
    highs = pd.to_numeric(panel["high"], errors="coerce").to_numpy(float)
    lows = pd.to_numeric(panel["low"], errors="coerce").to_numpy(float)
    closes = pd.to_numeric(panel["close"], errors="coerce").to_numpy(float)
    rows: List[Dict[str, Any]] = []
    i = start
    last_signal_i = min(end - 2, len(panel) - 2)
    while i <= last_signal_i:
        if not signal[i] or not valid_next[i]:
            i += 1; continue
        ent_i = i + 1
        if ent_i >= end:
            break
        entry = float(opens[ent_i])
        if not np.isfinite(entry) or entry <= 0:
            i += 1; continue
        atr = get_atr_abs(panel, i, entry)
        if not np.isfinite(atr) or atr <= 0:
            i += 1; continue
        initial_sl = entry + cfg.sl_atr * atr
        tp_price = entry - cfg.tp_atr * atr
        stop = initial_sl
        best_low = entry; worst_high = entry; trail_active = False
        exit_price = np.nan; reason = "TO"; exit_i = ent_i
        max_i = min(ent_i + cfg.hold_bars - 1, end - 1, len(panel) - 1)
        for j in range(ent_i, max_i + 1):
            hi, lo, cl = highs[j], lows[j], closes[j]
            if not np.isfinite(hi) or not np.isfinite(lo) or not np.isfinite(cl):
                continue
            best_low = min(best_low, lo); worst_high = max(worst_high, hi)
            favorable_atr = (entry - best_low) / atr
            if cfg.mode == "trail" and cfg.trail_trigger_atr is not None and favorable_atr >= cfg.trail_trigger_atr:
                trail_active = True
                trailing_stop = best_low + float(cfg.trail_dist_atr) * atr
                stop = min(stop, trailing_stop)
            hit_tp = lo <= tp_price; hit_stop = hi >= stop
            if hit_tp and hit_stop:
                if SAME_BAR_POLICY == "worst":
                    exit_price = stop; reason = "TR" if trail_active and stop < initial_sl else "SL"
                else:
                    exit_price = tp_price; reason = "TP"
                exit_i = j; break
            if hit_stop:
                exit_price = stop; reason = "TR" if trail_active and stop < initial_sl else "SL"; exit_i = j; break
            if hit_tp and cfg.mode == "fixed":
                exit_price = tp_price; reason = "TP"; exit_i = j; break
            if j == max_i:
                exit_price = cl; reason = "TO"; exit_i = j; break
        if not np.isfinite(exit_price):
            i += 1; continue
        gross = (entry - exit_price) / entry
        net = gross - ROUND_TRIP_COST
        mfe = max(0.0, (entry - best_low) / entry)
        mae = max(0.0, (worst_high - entry) / entry)
        et = pd.Timestamp(panel.at[ent_i, "ts_open"])
        rows.append({"engine": "SHORT", "side": "SHORT", "signal_i": i, "entry_i": ent_i, "exit_i": exit_i, "signal_time": panel.at[i, "ts_open"], "entry_time": et, "exit_time": panel.at[exit_i, "ts_open"], "entry": entry, "exit": float(exit_price), "gross": gross, "net": net, "cost": ROUND_TRIP_COST, "reason": reason, "bars": exit_i - ent_i + 1, "MFE": mfe, "MAE": mae, "MFE_MAE": safe_div(mfe, mae), "hour": int(et.hour), "dow": et.day_name(), "month": et.strftime("%Y-%m"), "quarter": f"{et.year}Q{et.quarter}", "year": str(et.year), "session": session_name(int(et.hour))})
        i = exit_i + 1 if NO_OVERLAP else i + 1
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame, split: str, signals: int = 0, period_days: Optional[int] = None) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"split": split, "signals": signals, "trades": 0, "tpd": 0.0, "wr": np.nan, "pf": np.nan, "gross": 0.0, "net": 0.0, "avgGross": np.nan, "avgNet": np.nan, "cost": 0.0, "maxDD": 0.0, "mfe": np.nan, "mae": np.nan, "mfe_mae": np.nan, "tp_rate": 0.0, "sl_rate": 0.0, "to_rate": 0.0, "tr_rate": 0.0, "bars": np.nan, "problem": "no_trades", "score": -9999.0}
    net = pd.to_numeric(trades["net"], errors="coerce").fillna(0.0)
    gross = pd.to_numeric(trades["gross"], errors="coerce").fillna(0.0)
    wins = net[net > 0].sum(); losses = -net[net < 0].sum()
    pf = np.inf if losses == 0 and wins > 0 else safe_div(wins, losses, 0.0)
    eq = net.cumsum(); dd = eq - eq.cummax(); maxdd = float(dd.min()) if len(dd) else 0.0
    if period_days is not None:
        days = max(1e-9, float(period_days))
    else:
        days = max(1e-9, (pd.Timestamp(trades["entry_time"].max()) - pd.Timestamp(trades["entry_time"].min())).total_seconds() / 86400.0 + 1.0)
    reasons = trades["reason"].astype(str).value_counts(normalize=True)
    out = {"split": split, "signals": int(signals), "trades": int(len(trades)), "tpd": float(len(trades) / days), "wr": float((net > 0).mean()), "pf": float(pf), "gross": float(gross.sum()), "net": float(net.sum()), "avgGross": float(gross.mean()), "avgNet": float(net.mean()), "cost": float(ROUND_TRIP_COST * len(trades)), "maxDD": maxdd, "mfe": float(pd.to_numeric(trades["MFE"], errors="coerce").mean()), "mae": float(pd.to_numeric(trades["MAE"], errors="coerce").mean()), "mfe_mae": float(pd.to_numeric(trades["MFE_MAE"], errors="coerce").mean()), "tp_rate": float(reasons.get("TP", 0.0)), "sl_rate": float(reasons.get("SL", 0.0)), "to_rate": float(reasons.get("TO", 0.0)), "tr_rate": float(reasons.get("TR", 0.0)), "bars": float(pd.to_numeric(trades["bars"], errors="coerce").mean())}
    if out["gross"] <= 0:
        problem = "no_gross_edge"
    elif out["net"] <= 0:
        problem = "cost_or_exit_kills_edge"
    elif np.isfinite(out["pf"]) and out["pf"] < 1.05:
        problem = "weak_profit_factor"
    else:
        problem = "pass"
    out["problem"] = problem
    pf_part = 0 if not np.isfinite(out["pf"]) else min(3.0, out["pf"]) * 2.0
    out["score"] = out["net"] * 100.0 + pf_part + out["wr"] * 5.0 + out["mfe_mae"] + out["maxDD"] * 80.0
    return out


def split_trades(trades: pd.DataFrame, sp: SplitDef) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    return trades[(trades["signal_i"] >= sp.start) & (trades["signal_i"] < sp.end)].copy()


def print_summary(label: str, s: Dict[str, Any]) -> None:
    print(f"  {label:<70} split={s['split']:<10} signals={s['signals']:>5} trades={s['trades']:>5} tpd={s['tpd']:>6.3f} WR={s['wr']*100:>6.2f}% PF={fmt_pf(s['pf']):>6} gross={s['gross']*100:>8.3f}% net={s['net']*100:>8.3f}% avgNet={s['avgNet']*10000:>7.2f}bp maxDD={s['maxDD']*100:>8.3f}% SL={s['sl_rate']*100:>6.2f}% TR={s['tr_rate']*100:>6.2f}% TO={s['to_rate']*100:>6.2f}% bars={s['bars']:>5.2f} problem={s['problem']:<26} score={s['score']:>9.4f}")


def eval_by_split(panel: pd.DataFrame, splits: Dict[str, SplitDef], mask: pd.Series, cfg: ExitConfig, side: str) -> Tuple[pd.DataFrame, Dict[str, Dict[str, Any]]]:
    frames = []; sums: Dict[str, Dict[str, Any]] = {}
    sim = simulate_long if side == "LONG" else simulate_short
    for name, sp in splits.items():
        trades = sim(panel, mask, cfg, sp.start, sp.end)
        frames.append(trades)
        valid_signals = int((mask.iloc[sp.start:sp.end].fillna(False) & panel["valid_next_entry"].iloc[sp.start:sp.end].fillna(False)).sum())
        period_days = period_days_from_panel(panel, sp.start, sp.end)
        sums[name] = summarize(trades, name, valid_signals, period_days=period_days)
    all_trades = pd.concat(frames, ignore_index=True) if frames and any(not f.empty for f in frames) else pd.DataFrame()
    return all_trades, sums

# ==============================================================================
# RULE ENGINES
# ==============================================================================

@dataclass
class RuleVariant:
    name: str
    side: str
    filter_name: str
    mask: pd.Series
    trades: pd.DataFrame
    sums: Dict[str, Dict[str, Any]]
    cfg: ExitConfig


# ==============================================================================
# V22 LONG — NATIVE VARIANT FROM USER-PROVIDED ORIGINAL LOGIC, NO SAVE
# ==============================================================================

V22_ORIGINAL_15M_FILE = BASE_DIR / "ETHUSDT_15m_BINANCE_20230401_20260401_clean_raw_plus_external.csv"
V22_V2_DIR = BASE_DIR / "new case study" / "long_orchestration_engine_v1" / "v2_management_backtest_from_separators"
V22_ROBUST_DIR = V22_V2_DIR / "selected_v2_robustness_audit"
V22_ENRICHED_TRADE_LOG_FILE = V22_ROBUST_DIR / "ethusdt_long_v2_selected_trade_log_enriched.csv"
V22_SELECTED_VARIANT_NAME = "V22_RX4_MIXED_BALANCED_CUT"

V22_PROBLEM_MONTHS = ["2025-06", "2025-08", "2025-09", "2025-11"]
V22_OK_MONTHS_REFERENCE = ["2025-07", "2025-10"]
V22_ROUND_TRIP_COST_BP = 12.0
V22_MIN_ATR_PRICE = 1e-9

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

V22_V21_MIXED_RESCUE_SL_ATR = 1.75
V22_V21_MIXED_RESCUE_TRAIL_START_ATR = 0.85
V22_V21_MIXED_RESCUE_TRAIL_DIST_ATR = 0.45

V22_WEAK_VOL_REGIMES = {"vol_mid", "vol_low"}
V22_WEAK_OI_REGIMES = {"oi_low_z", "oi_mid_z"}


@dataclass(frozen=True)
class V22Variant:
    name: str
    mode: str
    mixed_bad_close_max_atr: float
    mixed_bad_mfe_max_atr: float
    mixed_bad_mae_min_atr: float
    mixed_recover_mfe_min_atr: float
    mixed_recover_close_min_atr: float
    mixed_recover_sl_atr: float
    mixed_recover_trail_start_atr: float
    mixed_recover_trail_dist_atr: float
    mixed_bad_exit_at_bar4_close: bool


V22_VARIANTS: List[V22Variant] = [
    V22Variant("V22_CONTROL_V21_COMBINED_REBUILD", "control_v21", -999, -999, 999, 999, 999, V22_V21_MIXED_RESCUE_SL_ATR, V22_V21_MIXED_RESCUE_TRAIL_START_ATR, V22_V21_MIXED_RESCUE_TRAIL_DIST_ATR, False),
    V22Variant("V22_RX4_MIXED_STRICT_BAD_CUT", "strict_bad_cut", -0.70, 0.45, 1.25, 0.55, -0.60, 1.75, 0.85, 0.45, True),
    V22Variant("V22_RX4_MIXED_BALANCED_CUT", "balanced_bad_cut", -0.85, 0.55, 1.45, 0.55, -0.65, 1.80, 0.90, 0.45, True),
    V22Variant("V22_RX4_MIXED_RECOVER_PROTECT", "recover_protect", -1.05, 0.45, 1.65, 0.45, -0.80, 1.95, 0.95, 0.50, True),
]


def v22_norm_name(x: Any) -> str:
    return str(x).strip().lower().replace(" ", "_").replace("-", "_")


def v22_find_col(df: pd.DataFrame, candidates: Iterable[str], required: bool = True) -> Optional[str]:
    lookup = {v22_norm_name(c): c for c in df.columns}
    for cand in candidates:
        key = v22_norm_name(cand)
        if key in lookup:
            return lookup[key]
    if required:
        raise KeyError(f"Missing required column. Tried={list(candidates)} | sample={list(df.columns)[:40]}")
    return None


def v22_to_utc_naive(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(None)


def v22_safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def v22_calc_pf(net_bp: pd.Series) -> float:
    x = pd.to_numeric(net_bp, errors="coerce").dropna()
    wins = float(x[x > 0].sum())
    losses = float(x[x < 0].sum())
    if losses < 0:
        return wins / abs(losses)
    if wins > 0:
        return np.inf
    return np.nan


def v22_max_dd_pct_from_bp(net_bp: pd.Series) -> float:
    x = pd.to_numeric(net_bp, errors="coerce").fillna(0.0)
    if len(x) == 0:
        return 0.0
    equity = 1.0 + (x.to_numpy(dtype=float) / 10000.0).cumsum()
    peak = np.maximum.accumulate(equity)
    dd = equity / np.where(peak == 0, np.nan, peak) - 1.0
    return float(np.nanmin(dd) * 100.0)


def v22_summarize(df: pd.DataFrame, keys: List[str]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame()
    for vals, g in df.groupby(keys, dropna=False):
        if not isinstance(vals, tuple):
            vals = (vals,)
        v22 = pd.to_numeric(g["v22_net_bp"], errors="coerce")
        v21 = pd.to_numeric(g["current_v21_net_bp"], errors="coerce")
        v2 = pd.to_numeric(g["current_v2_net_bp"], errors="coerce")
        v1 = pd.to_numeric(g["original_net_bp"], errors="coerce")
        exit_bars = pd.to_numeric(g["v22_exit_bars"], errors="coerce")
        d: Dict[str, Any] = dict(zip(keys, vals))
        d.update(
            trades=int(len(g)),
            v22_net_pct=float(v22.sum() / 100.0),
            v22_avg_bp=float(v22.mean()),
            v22_pf=v22_calc_pf(v22),
            v22_win_rate=float((v22 > 0).mean()),
            v22_max_dd_pct=v22_max_dd_pct_from_bp(v22),
            v22_fast_loss_rate=float(((v22 < 0) & (exit_bars <= 3)).mean()),
            current_v21_net_pct=float(v21.sum() / 100.0),
            current_v21_avg_bp=float(v21.mean()),
            current_v21_pf=v22_calc_pf(v21),
            delta_vs_v21_net_pct=float((v22.sum() - v21.sum()) / 100.0),
            delta_vs_v21_avg_bp=float((v22 - v21).mean()),
            current_v2_net_pct=float(v2.sum() / 100.0),
            delta_vs_v2_net_pct=float((v22.sum() - v2.sum()) / 100.0),
            original_v1_net_pct=float(v1.sum() / 100.0),
            delta_vs_original_v1_net_pct=float((v22.sum() - v1.sum()) / 100.0),
            trail_rate=float((g["v22_exit_reason"] == "V22_TRAIL").mean()),
            normal_sl_rate=float((g["v22_exit_reason"] == "V22_NORMAL_SL").mean()),
            provisional_sl_rate=float((g["v22_exit_reason"] == "V22_PROVISIONAL_SL").mean()),
            mixed_bad_exit_rate=float((g["v22_exit_reason"] == "V22_MIXED_BAD_EXIT").mean()),
            true_failure_exit_rate=float((g["v22_exit_reason"] == "V22_TRUE_FAILURE_EXIT").mean()),
            time_exit_rate=float((g["v22_exit_reason"] == "V22_TIME_EXIT").mean()),
            tp_rate=float((g["v22_exit_reason"] == "V22_TP").mean()),
        )
        rows.append(d)
    return pd.DataFrame(rows)


def v22_load_original_panel() -> pd.DataFrame:
    section("V22 LONG — LOAD ORIGINAL 15M PANEL")
    if not V22_ORIGINAL_15M_FILE.exists():
        raise FileNotFoundError(f"Missing original 15m file: {V22_ORIGINAL_15M_FILE}")
    df = pd.read_csv(V22_ORIGINAL_15M_FILE, low_memory=False)
    time_col = v22_find_col(df, ["date", "datetime", "timestamp", "open_time"], required=False)
    if time_col is not None:
        df["_time"] = v22_to_utc_naive(df[time_col])
    df = df.sort_values("_time").reset_index(drop=True) if "_time" in df.columns else df.reset_index(drop=True)
    df["_panel_pos"] = np.arange(len(df), dtype=int)
    print(f"v22_original_panel rows={len(df):,} cols={len(df.columns):,}")
    return df


def v22_load_enriched_trades() -> pd.DataFrame:
    section("V22 LONG — LOAD SELECTED V2 ENRICHED TRADES")
    if not V22_ENRICHED_TRADE_LOG_FILE.exists():
        raise FileNotFoundError(f"Missing selected enriched trade log: {V22_ENRICHED_TRADE_LOG_FILE}")
    df = pd.read_csv(V22_ENRICHED_TRADE_LOG_FILE, low_memory=False)
    if "month" not in df.columns:
        df["entry_time_dt"] = v22_to_utc_naive(df["entry_time"])
        df["month"] = df["entry_time_dt"].dt.strftime("%Y-%m")
    df = df[df["pre_entry_archetype"].astype(str).str.lower().eq("breakout")].copy()
    df["current_v2_net_bp"] = pd.to_numeric(df["v2_net_bp"], errors="coerce")
    print(f"v22_enriched_breakout_trades rows={len(df):,} cols={len(df.columns):,}")
    print("v22 split counts:")
    print(df["split"].value_counts(dropna=False).to_string())
    return df


def v22_classify_rx4(row: pd.Series) -> str:
    close = v22_safe_float(row.get("rx4_close_atr", np.nan))
    mfe = v22_safe_float(row.get("rx4_mfe_atr", np.nan))
    mae = v22_safe_float(row.get("rx4_mae_atr", np.nan))
    if np.isfinite(mfe) and np.isfinite(close) and np.isfinite(mae):
        if mfe >= 1.0 and close >= 0.0 and mae <= 1.0:
            return "rx4_runner_like"
        if mfe <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR and mae >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR:
            return "rx4_true_failure_like"
        if mfe >= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close > V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR:
            return "rx4_false_sl_like"
    return "rx4_mixed"


def v22_true_failure_decision(row: pd.Series) -> bool:
    close = v22_safe_float(row.get("rx4_close_atr", np.nan))
    mfe = v22_safe_float(row.get("rx4_mfe_atr", np.nan))
    mae = v22_safe_float(row.get("rx4_mae_atr", np.nan))
    return bool(np.isfinite(close) and np.isfinite(mfe) and np.isfinite(mae) and mfe <= V22_BASE_TRUE_FAIL_MFE_MAX_ATR and close <= V22_BASE_TRUE_FAIL_CLOSE_MAX_ATR and mae >= V22_BASE_TRUE_FAIL_MAE_MIN_ATR)


def v22_mixed_bad_decision(row: pd.Series, variant: V22Variant) -> bool:
    if variant.mode == "control_v21":
        return False
    if str(row.get("rx4_class", "")) != "rx4_mixed":
        return False
    close = v22_safe_float(row.get("rx4_close_atr", np.nan))
    mfe = v22_safe_float(row.get("rx4_mfe_atr", np.nan))
    mae = v22_safe_float(row.get("rx4_mae_atr", np.nan))
    return bool(np.isfinite(close) and np.isfinite(mfe) and np.isfinite(mae) and close <= variant.mixed_bad_close_max_atr and mfe <= variant.mixed_bad_mfe_max_atr and mae >= variant.mixed_bad_mae_min_atr)


def v22_mixed_recover_decision(row: pd.Series, variant: V22Variant) -> bool:
    if str(row.get("rx4_class", "")) != "rx4_mixed":
        return False
    close = v22_safe_float(row.get("rx4_close_atr", np.nan))
    mfe = v22_safe_float(row.get("rx4_mfe_atr", np.nan))
    return bool(np.isfinite(close) and np.isfinite(mfe) and (mfe >= variant.mixed_recover_mfe_min_atr or close >= variant.mixed_recover_close_min_atr))


def v22_provisional_sl_atr(row: pd.Series) -> float:
    vol = str(row.get("regime_volatility", "")).lower()
    oi = str(row.get("regime_oi_z20", "")).lower()
    if vol in V22_WEAK_VOL_REGIMES or oi in V22_WEAK_OI_REGIMES:
        return V22_BASE_PROVISIONAL_SL_ATR
    return V22_BASE_PROVISIONAL_SL_DEFAULT_ATR


def v22_management_params(row: pd.Series, variant: V22Variant, bar_no: int) -> Tuple[float, float, float]:
    rx4_class = str(row.get("rx4_class", ""))
    if rx4_class == "rx4_false_sl_like":
        return 1.55, V22_BASE_TRAIL_START_ATR, V22_BASE_TRAIL_DIST_ATR
    if rx4_class == "rx4_mixed" and bar_no <= 8:
        if variant.mode == "control_v21":
            return 1.75, 0.85, 0.45
        if v22_mixed_recover_decision(row, variant):
            return variant.mixed_recover_sl_atr, variant.mixed_recover_trail_start_atr, variant.mixed_recover_trail_dist_atr
        return 1.75, 0.85, 0.45
    return V22_BASE_NORMAL_SL_ATR, V22_BASE_TRAIL_START_ATR, V22_BASE_TRAIL_DIST_ATR


def v22_simulate_one(panel: pd.DataFrame, row: pd.Series, variant: V22Variant) -> Dict[str, Any]:
    entry_pos = int(row["entry_pos"])
    if entry_pos < 0 or entry_pos >= len(panel):
        return {}
    entry_price = v22_safe_float(row.get("entry_price", np.nan))
    if not np.isfinite(entry_price) or entry_price <= 0:
        entry_price = v22_safe_float(panel.iloc[entry_pos].get("open", np.nan))
    atr = v22_safe_float(row.get("atr_price", np.nan))
    if not np.isfinite(atr) or atr <= V22_MIN_ATR_PRICE:
        atr = max(entry_price * 0.005, V22_MIN_ATR_PRICE)
    prov_atr = v22_provisional_sl_atr(row)
    prov_stop = entry_price - prov_atr * atr
    tp_price = entry_price + V22_BASE_TP_ATR * atr
    highest = entry_price
    trail_active = False
    trail_stop = np.nan
    exit_pos = min(entry_pos + V22_BASE_MAX_HOLD_BARS - 1, len(panel) - 1)
    exit_price = v22_safe_float(panel.iloc[exit_pos].get("close", np.nan))
    exit_reason = "V22_TIME_EXIT"
    exit_bars = V22_BASE_MAX_HOLD_BARS
    v22_mixed_decision = "not_mixed"
    if str(row.get("rx4_class", "")) == "rx4_mixed":
        if v22_mixed_bad_decision(row, variant):
            v22_mixed_decision = "mixed_bad"
        elif v22_mixed_recover_decision(row, variant):
            v22_mixed_decision = "mixed_recover"
        else:
            v22_mixed_decision = "mixed_unclear"
    for bar_no in range(1, V22_BASE_MAX_HOLD_BARS + 1):
        pos = entry_pos + bar_no - 1
        if pos >= len(panel):
            break
        bar = panel.iloc[pos]
        high = v22_safe_float(bar.get("high", np.nan))
        low = v22_safe_float(bar.get("low", np.nan))
        close = v22_safe_float(bar.get("close", np.nan))
        highest = max(highest, high)
        if bar_no <= V22_BASE_DECISION_BAR:
            if np.isfinite(low) and low <= prov_stop:
                exit_pos = pos; exit_price = prov_stop; exit_reason = "V22_PROVISIONAL_SL"; exit_bars = bar_no; break
            if bar_no == V22_BASE_DECISION_BAR:
                if v22_true_failure_decision(row):
                    exit_pos = pos; exit_price = close; exit_reason = "V22_TRUE_FAILURE_EXIT"; exit_bars = bar_no; break
                if variant.mixed_bad_exit_at_bar4_close and v22_mixed_bad_decision(row, variant):
                    exit_pos = pos; exit_price = close; exit_reason = "V22_MIXED_BAD_EXIT"; exit_bars = bar_no; break
            continue
        normal_sl_atr, trail_start_atr, trail_dist_atr = v22_management_params(row, variant, bar_no)
        normal_sl = entry_price - normal_sl_atr * atr
        if not trail_active and highest >= entry_price + trail_start_atr * atr:
            trail_active = True
            trail_stop = highest - trail_dist_atr * atr
        elif trail_active:
            trail_stop = max(trail_stop, highest - trail_dist_atr * atr)
        if np.isfinite(low) and low <= normal_sl:
            exit_pos = pos; exit_price = normal_sl; exit_reason = "V22_NORMAL_SL"; exit_bars = bar_no; break
        if trail_active and np.isfinite(low) and low <= trail_stop:
            exit_pos = pos; exit_price = trail_stop; exit_reason = "V22_TRAIL"; exit_bars = bar_no; break
        if np.isfinite(high) and high >= tp_price:
            exit_pos = pos; exit_price = tp_price; exit_reason = "V22_TP"; exit_bars = bar_no; break
    net_bp = ((exit_price / entry_price) - 1.0) * 10000.0 - V22_ROUND_TRIP_COST_BP
    return {"v22_exit_pos": exit_pos, "v22_exit_time": panel.iloc[exit_pos].get("_time", pd.NaT) if 0 <= exit_pos < len(panel) else pd.NaT, "v22_exit_price": exit_price, "v22_exit_reason": exit_reason, "v22_exit_bars": exit_bars, "v22_net_bp": net_bp, "v22_provisional_sl_atr_used": prov_atr, "v22_mixed_decision": v22_mixed_decision}


def v22_run_variants(panel: pd.DataFrame, trades: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    section("V22 LONG — RUN TARGETED VARIANTS FROM ORIGINAL LOGIC")
    base = trades.copy()
    base["rx4_class"] = base.apply(v22_classify_rx4, axis=1)
    all_logs: List[pd.DataFrame] = []
    for variant in V22_VARIANTS:
        print(f"Running {variant.name}")
        rows: List[Dict[str, Any]] = []
        for _, r in base.iterrows():
            sim = v22_simulate_one(panel, r, variant)
            if not sim:
                continue
            d = {
                "variant": variant.name,
                "trade_id": r.get("trade_id"),
                "split": r.get("split"),
                "month": r.get("month"),
                "entry_time": r.get("entry_time"),
                "entry_pos": r.get("entry_pos"),
                "entry_price": r.get("entry_price"),
                "atr_price": r.get("atr_price"),
                "pre_entry_archetype": r.get("pre_entry_archetype"),
                "regime_trend_state": r.get("regime_trend_state"),
                "regime_volatility": r.get("regime_volatility"),
                "regime_oi_z20": r.get("regime_oi_z20"),
                "regime_reaction4": r.get("regime_reaction4"),
                "rx4_class": r.get("rx4_class"),
                "audit_class": r.get("audit_class"),
                "original_exit_reason": r.get("original_exit_reason"),
                "original_net_bp": r.get("original_net_bp"),
                "current_v2_exit_reason": r.get("v2_exit_reason"),
                "current_v2_net_bp": r.get("current_v2_net_bp"),
                "future_good": r.get("future_good"),
                "future_runner": r.get("future_runner"),
                "rx4_close_atr": r.get("rx4_close_atr"),
                "rx4_mfe_atr": r.get("rx4_mfe_atr"),
                "rx4_mae_atr": r.get("rx4_mae_atr"),
                "pre_oi_open_interest_z_20": r.get("pre_oi_open_interest_z_20"),
            }
            d.update(sim)
            rows.append(d)
        all_logs.append(pd.DataFrame(rows))
    log = pd.concat(all_logs, ignore_index=True)
    control = log[log["variant"].eq("V22_CONTROL_V21_COMBINED_REBUILD")][["trade_id", "v22_net_bp"]].rename(columns={"v22_net_bp": "current_v21_net_bp"})
    log = log.merge(control, on="trade_id", how="left")
    split_summary = v22_summarize(log, ["variant", "split"])
    trainval = log[log["split"].isin(["train", "validation"])].copy()
    trainval_summary = v22_summarize(trainval, ["variant"])
    val = log[log["split"].eq("validation")].copy()
    val["validation_month_group"] = np.where(val["month"].isin(V22_PROBLEM_MONTHS), "PROBLEM_MONTHS", np.where(val["month"].isin(V22_OK_MONTHS_REFERENCE), "OK_MONTHS", "OTHER_VALIDATION"))
    target = val[val["validation_month_group"].isin(["PROBLEM_MONTHS", "OK_MONTHS"])].copy()
    problem_ok = v22_summarize(target, ["variant", "validation_month_group"])
    problem = problem_ok[problem_ok["validation_month_group"].eq("PROBLEM_MONTHS")][["variant", "v22_net_pct", "v22_pf", "delta_vs_v21_net_pct"]].rename(columns={"v22_net_pct": "problem_months_net_pct", "v22_pf": "problem_months_pf", "delta_vs_v21_net_pct": "problem_months_delta_vs_v21_net_pct"})
    ok = problem_ok[problem_ok["validation_month_group"].eq("OK_MONTHS")][["variant", "v22_net_pct", "delta_vs_v21_net_pct"]].rename(columns={"v22_net_pct": "ok_months_net_pct", "delta_vs_v21_net_pct": "ok_months_delta_vs_v21_net_pct"})
    selection = trainval_summary.merge(problem, on="variant", how="left").merge(ok, on="variant", how="left")
    selection["selection_score"] = (
        selection["v22_avg_bp"].fillna(-999)
        + 5.0 * np.log(selection["v22_pf"].replace(np.inf, 10.0).clip(lower=0.01))
        + 0.90 * selection["problem_months_delta_vs_v21_net_pct"].fillna(0)
        - 0.35 * np.maximum(0, -selection["ok_months_delta_vs_v21_net_pct"].fillna(0))
        - 10.0 * selection["v22_fast_loss_rate"].fillna(1.0)
    )
    selection = selection.sort_values(["selection_score", "problem_months_delta_vs_v21_net_pct", "v22_pf"], ascending=[False, False, False]).reset_index(drop=True)
    selection["selected_trainval_only"] = False
    if len(selection):
        selection.loc[0, "selected_trainval_only"] = True
    selected_variant = selection.loc[0, "variant"] if len(selection) else "NONE"
    print("V22 train+validation selection:")
    print(selection.to_string(index=False))
    print(f"V22_SELECTED_VARIANT={selected_variant}")
    return log, split_summary, selection, selected_variant


def v22_reason_to_baseline_reason(x: Any) -> str:
    s = str(x)
    if s == "V22_TRAIL":
        return "TR"
    if s == "V22_TP":
        return "TP"
    if s == "V22_TIME_EXIT":
        return "TO"
    return "SL"


def build_v22_long_variant(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> RuleVariant:
    section("BUILD V22 LONG VARIANT INSIDE BASELINE STRUCTURE — ORIGINAL V22 LOGIC")
    original_panel = v22_load_original_panel()
    enriched_trades = v22_load_enriched_trades()
    log, split_summary, selection, selected_variant = v22_run_variants(original_panel, enriched_trades)
    if selected_variant != V22_SELECTED_VARIANT_NAME:
        die(f"V22 selected variant changed unexpectedly: selected={selected_variant}, expected={V22_SELECTED_VARIANT_NAME}")
    selected_log = log[log["variant"].eq(V22_SELECTED_VARIANT_NAME)].copy().reset_index(drop=True)
    if selected_log.empty:
        die("V22 selected log is empty.")
    time_to_i = pd.Series(panel.index.to_numpy(), index=pd.to_datetime(panel["ts_open"]).to_numpy()).to_dict()
    entry_time = v22_to_utc_naive(selected_log["entry_time"])
    exit_time = v22_to_utc_naive(selected_log["v22_exit_time"])
    entry_i = entry_time.map(time_to_i)
    exit_i = exit_time.map(time_to_i)
    bad = entry_i.isna() | exit_i.isna()
    print(f"v22_timestamp_mapping_bad_rows={int(bad.sum()):,}")
    if bad.any():
        print(selected_log.loc[bad, ["entry_time", "v22_exit_time"]].head(20).to_string(index=False))
        die("V22 timestamp mapping failed against baseline panel.")
    rows: List[Dict[str, Any]] = []
    highs = pd.to_numeric(panel["high"], errors="coerce").to_numpy(float)
    lows = pd.to_numeric(panel["low"], errors="coerce").to_numpy(float)
    for idx, r in selected_log.iterrows():
        ent_i = int(entry_i.iloc[idx])
        ex_i = int(exit_i.iloc[idx])
        sig_i = ent_i - 1
        if sig_i < 0:
            continue
        entry = float(r["entry_price"])
        exit_price = float(r["v22_exit_price"])
        gross = exit_price / entry - 1.0
        net = float(r["v22_net_bp"]) / 10000.0
        if ex_i >= ent_i:
            best_high = np.nanmax(highs[ent_i:ex_i + 1])
            worst_low = np.nanmin(lows[ent_i:ex_i + 1])
        else:
            best_high = entry
            worst_low = entry
        mfe = best_high / entry - 1.0 if np.isfinite(best_high) and entry > 0 else np.nan
        mae = entry / worst_low - 1.0 if np.isfinite(worst_low) and worst_low > 0 else np.nan
        et = pd.Timestamp(panel.at[ent_i, "ts_open"])
        rows.append({
            "engine": "V22_LONG",
            "side": "LONG",
            "signal_i": sig_i,
            "entry_i": ent_i,
            "exit_i": ex_i,
            "signal_time": panel.at[sig_i, "ts_open"],
            "entry_time": et,
            "exit_time": panel.at[ex_i, "ts_open"],
            "entry": entry,
            "exit": exit_price,
            "gross": gross,
            "net": net,
            "cost": ROUND_TRIP_COST,
            "reason": v22_reason_to_baseline_reason(r["v22_exit_reason"]),
            "bars": int(r["v22_exit_bars"]),
            "MFE": mfe,
            "MAE": mae,
            "MFE_MAE": safe_div(mfe, mae),
            "hour": int(et.hour),
            "dow": et.day_name(),
            "month": et.strftime("%Y-%m"),
            "quarter": f"{et.year}Q{et.quarter}",
            "year": str(et.year),
            "session": session_name(int(et.hour)),
        })
    trades = pd.DataFrame(rows).sort_values(["signal_i", "entry_i", "exit_i"]).reset_index(drop=True)
    mask = pd.Series(False, index=panel.index)
    if not trades.empty:
        mask.iloc[trades["signal_i"].astype(int).to_numpy()] = True
    sums: Dict[str, Dict[str, Any]] = {}
    for name, sp in splits.items():
        part = split_trades(trades, sp)
        signals = int(mask.iloc[sp.start:sp.end].sum())
        sums[name] = summarize(part, name, signals, period_days=period_days_from_panel(panel, sp.start, sp.end))
    cfg = ExitConfig(V22_SELECTED_VARIANT_NAME, "v22_original_logic", np.nan, np.nan, V22_BASE_MAX_HOLD_BARS, np.nan, np.nan)
    variant = RuleVariant(V22_SELECTED_VARIANT_NAME, "LONG", V22_SELECTED_VARIANT_NAME, mask, trades, sums, cfg)
    subsection("V22 LONG VARIANT INSIDE BASELINE STRUCTURE — SUMMARY")
    for sp in ["train", "validation", "test"]:
        print_summary("V22_LONG_NATIVE", sums[sp])
    full = summarize(trades, "full_2y", int(mask.sum()), period_days=period_days_from_panel(panel, 0, len(panel)))
    print_summary("V22_LONG_NATIVE", full)
    return variant


def summarize_combined_variant_by_split(panel: pd.DataFrame, long_variant: RuleVariant, short_variant: RuleVariant) -> Dict[str, Dict[str, Any]]:
    combined = pd.concat([long_variant.trades, short_variant.trades], ignore_index=True).sort_values(["signal_i", "side"]).reset_index(drop=True)
    out: Dict[str, Dict[str, Any]] = {}
    for name, sp in make_splits(panel).items():
        part = split_trades(combined, sp)
        out[name] = summarize(part, name, 0, period_days=period_days_from_panel(panel, sp.start, sp.end))
    out["full_2y"] = summarize(combined, "full_2y", 0, period_days=period_days_from_panel(panel, 0, len(panel)))
    return out


def print_old_vs_v22_comparison(title: str, old_sums: Dict[str, Dict[str, Any]], v22_sums: Dict[str, Dict[str, Any]]) -> None:
    section(title)
    print(f"{'split':<12} {'old_trades':>10} {'v22_trades':>10} {'old_net':>11} {'v22_net':>11} {'delta_net':>11} {'old_PF':>8} {'v22_PF':>8} {'old_DD':>10} {'v22_DD':>10} {'old_avg':>10} {'v22_avg':>10}")

    def trade_count(s: Dict[str, Any]) -> int:
        # RAW summaries use "trades"; ML summaries use "taken".
        return int(s.get("trades", s.get("taken", 0)))

    for sp in ["train", "validation", "test", "full_2y"]:
        o = old_sums[sp]
        n = v22_sums[sp]
        print(f"{sp:<12} {trade_count(o):>10,} {trade_count(n):>10,} {o['net']*100:>10.3f}% {n['net']*100:>10.3f}% {(n['net']-o['net'])*100:>10.3f}% {fmt_pf(o['pf']):>8} {fmt_pf(n['pf']):>8} {o['maxDD']*100:>9.3f}% {n['maxDD']*100:>9.3f}% {o['avgNet']*10000:>9.2f}bp {n['avgNet']*10000:>9.2f}bp")


def ml_selected_sums(ml_result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    selected = ml_result["selected_variant"]
    return ml_result["variant_results"][selected]["combined_sums"]



def finite_threshold(v: float, fallback: float) -> float:
    return float(v) if np.isfinite(v) else float(fallback)


def build_long_adx_breakout(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> pd.Series:
    section("LONG ENGINE — BUILD LOCKED TRIGGER: adx_breakout — NEW RULES CLOSE_POS Q50 + ADX Q55")
    close_pos_col = first_col(panel, ["close_pos", "close_position"])
    mom_col = first_col(panel, ["mom", "momentum"])
    adx_col = first_col(panel, ["adx_14"], required=True, label="LONG adx_14")
    di_col = first_col(panel, ["di_diff_14"], required=True, label="LONG di_diff_14")
    ms_up_col = first_col(panel, ["ms_break_up", "break_up", "sr_break_up"])
    close_pos_q50 = finite_threshold(qtrain(panel, splits, close_pos_col, 0.50), 0.50)
    mom_q70 = qtrain(panel, splits, mom_col, 0.70, True)
    adx_q55 = qtrain(panel, splits, adx_col, 0.55, True)
    di_q60 = qtrain(panel, splits, di_col, 0.60, True)
    print(f"adx_q55={adx_q55:.6g} di_q60={di_q60:.6g} close_pos_q50={close_pos_q50:.6g} mom_q70={mom_q70:.6g}")
    m = ((num(panel, adx_col) >= adx_q55) & (num(panel, di_col) >= di_q60) & (num(panel, close_pos_col) >= close_pos_q50) & (bool_col(panel, ms_up_col) | (num(panel, mom_col) >= mom_q70))).fillna(False)
    mask_rate("LONG trigger__adx_breakout_new_rules_close_pos_q50_adx_q55", m)
    return m


def build_1h_long_soft_veto(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> pd.Series:
    section("LONG ENGINE — CONTEXT 1H SOFT VETO — RELAXED V1")
    adx_col = first_col(panel, ["adx_14_1h", "1h__adx_14"])
    di_col = first_col(panel, ["di_diff_14_1h", "1h__di_diff_14"])
    rsi_col = first_col(panel, ["rsi_14_1h", "1h__rsi_14"])
    print(f"adx_1h_col={adx_col} di_1h_col={di_col} rsi_1h_col={rsi_col}")
    if not USE_1H_SOFT_VETO or adx_col is None or di_col is None or rsi_col is None:
        print("1h soft veto disabled or missing columns -> pass all.")
        return pd.Series(True, index=panel.index)
    adx_q80 = qtrain(panel, splits, adx_col, 0.80)
    di_q20 = qtrain(panel, splits, di_col, 0.20)
    rsi_q20 = qtrain(panel, splits, rsi_col, 0.20)
    print(f"RELAXED_LONG_SOFT_VETO: adx_q80={adx_q80:.4f} di_low_q20={di_q20:.4f} rsi_low_q20={rsi_q20:.4f}")
    bearish = (num(panel, adx_col) >= adx_q80) & (num(panel, di_col) <= di_q20) & (num(panel, rsi_col) <= rsi_q20)
    m = (~bearish).fillna(True)
    mask_rate("LONG relaxed_soft_veto_pass", m)
    return m


def build_long_final_filter(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> Tuple[str, pd.Series]:
    di_col = first_col(panel, ["di_diff_14"], required=True, label="LONG final DI")
    di60 = qtrain(panel, splits, di_col, 0.60, True)
    hour = panel["ts_open"].dt.hour
    m = ((num(panel, di_col) >= di60) & (hour >= 0) & (hour < 22)).fillna(False)
    name = f"di15m_bull>=0.60q({di60:.6g}) AND session_not_late_00_22"
    mask_rate(f"LONG_FINAL_FILTER={name}", m)
    return name, m


def run_long_locked(panel: pd.DataFrame, splits: Dict[str, SplitDef], specs: List[FeatureSpec]) -> RuleVariant:
    section("LONG ENGINE START — LOCKED CURRENT LONG")
    print("LONG is kept fixed. No new long expansion/search is done.")
    setup = build_family_mask(panel, specs, "LONG", LONG_SETUP_FAMILY, "LONG ENGINE")
    trigger = build_long_adx_breakout(panel, splits)
    veto = build_1h_long_soft_veto(panel, splits)
    filter_name, final_filter = build_long_final_filter(panel, splits)
    cfg = ExitConfig(LONG_EXIT_NAME, "trail", LONG_EXIT_SL_ATR, LONG_EXIT_TP_ATR, LONG_EXIT_MAX_HOLD_BARS, LONG_EXIT_TRAIL_START_ATR, LONG_EXIT_TRAIL_DIST_ATR)
    mask = (setup & trigger & veto & final_filter & panel["valid_next_entry"]).fillna(False)
    trades, sums = eval_by_split(panel, splits, mask, cfg, "LONG")
    subsection("LONG CURRENT RESULT")
    for sp in ["train", "validation", "test"]:
        print_summary(sp, sums[sp])
    print_summary("full_2y", summarize(trades, "full_2y", int(mask.sum()), period_days=period_days_from_panel(panel, 0, len(panel))))
    return RuleVariant("LONG_CURRENT", "LONG", filter_name, mask, trades, sums, cfg)


def build_momentum_break_short(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> pd.Series:
    section("SHORT ENGINE — BUILD LOCKED 15M ENTRY TRIGGER: momentum_break — RELAXED V3 RANGE/BODY Q50")
    cols = ["range", "body_pct", "mom", "s1_mom", "vol_z_20", "close", "open"]
    require_cols(panel, cols, "SHORT momentum_break")
    range_q50 = qtrain(panel, splits, "range", 0.50, True)
    body_q50 = qtrain(panel, splits, "body_pct", 0.50, True)
    mom_q30 = qtrain(panel, splits, "mom", 0.30, True)
    s1_q30 = qtrain(panel, splits, "s1_mom", 0.30, True)
    vol_q60 = qtrain(panel, splits, "vol_z_20", 0.60, True)
    print(f"RELAXED_SHORT_TRIGGER_V3: range_q50={range_q50:.6f} body_pct_q50={body_q50:.6f} mom_q30={mom_q30:.6f} s1_mom_q30={s1_q30:.6f} vol_z_20_q60={vol_q60:.6f}")
    mask = ((num(panel, "close") < num(panel, "open")) & (num(panel, "range") >= range_q50) & (num(panel, "body_pct") >= body_q50) & ((num(panel, "mom") <= mom_q30) | (num(panel, "s1_mom") <= s1_q30)) & (num(panel, "vol_z_20") >= vol_q60)).fillna(False)
    mask_rate("SHORT trigger__momentum_break_relaxed_v3_range_body_q50", mask)
    return mask


def build_1h_soft_veto_short(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> pd.Series:
    section("SHORT ENGINE — CONTEXT 1H SOFT VETO — RELAXED V1")
    if not USE_1H_SOFT_VETO:
        print("USE_1H_SOFT_VETO=False")
        return pd.Series(True, index=panel.index)
    adx_col = first_col(panel, ["adx_14_1h", "1h__adx_14"], required=True, label="1H adx")
    di_col = first_col(panel, ["di_diff_14_1h", "1h__di_diff_14"], required=True, label="1H di")
    rsi_col = first_col(panel, ["rsi_14_1h", "1h__rsi_14"], required=True, label="1H rsi")
    adx_q80 = qtrain(panel, splits, adx_col, 0.80, True)
    di_q80 = qtrain(panel, splits, di_col, 0.80, True)
    rsi_q80 = qtrain(panel, splits, rsi_col, 0.80, True)
    print(f"adx_1h_col={adx_col} di_1h_col={di_col} rsi_1h_col={rsi_col}")
    print(f"RELAXED_SHORT_SOFT_VETO: adx_q80={adx_q80:.4f} di_high_q80={di_q80:.4f} rsi_high_q80={rsi_q80:.4f}")
    bullish_1h = (num(panel, adx_col) >= adx_q80) & (num(panel, di_col) >= di_q80) & (num(panel, rsi_col) >= rsi_q80)
    pass_mask = (~bullish_1h).fillna(True)
    mask_rate("SHORT relaxed_soft_veto_pass", pass_mask)
    return pass_mask


@dataclass
class FilterDef:
    name: str
    mask: pd.Series


def build_short_filters(panel: pd.DataFrame, splits: Dict[str, SplitDef]) -> List[FilterDef]:
    section("SHORT ENGINE — BUILD EXPANSION FILTERS")
    adx_col = "adx_14"
    slope_col = SHORT_EMA_SLOPE_FILTER_COL_REQUIRED
    require_cols(panel, [adx_col, slope_col], "SHORT expansion filters")
    adx70 = qtrain(panel, splits, adx_col, 0.70, True)
    adx60 = qtrain(panel, splits, adx_col, 0.60, True)
    slope30 = qtrain(panel, splits, slope_col, 0.30, True)
    print(f"adx15m_q70={adx70:.6f}")
    print(f"adx15m_q60={adx60:.6f}")
    print(f"emaSlope15m_bear_q30={slope30:.6f}")
    adx = num(panel, adx_col); slope = num(panel, slope_col)
    filters = [
        FilterDef("NO_FILTER", pd.Series(True, index=panel.index)),
    ]
    for f in filters:
        mask_rate(f"filter={f.name}", f.mask)
    print("Plan filter available: VERSION_B / SHORT_NO_FILTER only.")
    return filters


def find_filter_by_variant(filters: List[FilterDef], variant: str) -> FilterDef:
    if variant == "SHORT_NO_FILTER":
        for f in filters:
            if f.name == "NO_FILTER":
                return f
    die(f"Missing Version B filter for variant={variant}")


def run_short_expansion(panel: pd.DataFrame, splits: Dict[str, SplitDef], specs: List[FeatureSpec]) -> Dict[str, RuleVariant]:
    section("SHORT ENGINE START — VERSION B ONLY")
    print("SHORT setup and trigger are unchanged. Version B uses NO_FILTER only.")
    setup = build_family_mask(panel, specs, "SHORT", SHORT_LOCKED_SETUP_FAMILY, "SHORT ENGINE")
    trigger = build_momentum_break_short(panel, splits)
    veto = build_1h_soft_veto_short(panel, splits)
    locked_signal = (setup & trigger & veto & panel["valid_next_entry"]).fillna(False)
    print(f"locked_short_signals_total={int(locked_signal.sum()):,}/{len(panel):,} rate={locked_signal.mean()*100:.3f}%")
    filters = build_short_filters(panel, splits)
    cfg = ExitConfig(SHORT_EXIT_NAME, "trail", SHORT_EXIT_SL_ATR, SHORT_EXIT_TP_ATR, SHORT_EXIT_MAX_HOLD_BARS, SHORT_EXIT_TRAIL_START_ATR, SHORT_EXIT_TRAIL_DIST_ATR)
    variants: Dict[str, RuleVariant] = {}
    for variant in SHORT_EXPANSION_VARIANTS:
        f = find_filter_by_variant(filters, variant)
        mask = (locked_signal & f.mask & panel["valid_next_entry"]).fillna(False)
        trades, sums = eval_by_split(panel, splits, mask, cfg, "SHORT")
        variants[variant] = RuleVariant(variant, "SHORT", f.name, mask, trades, sums, cfg)
        subsection(f"RAW VERSION B: {variant}")
        print(f"filter={f.name}")
        for sp in ["train", "validation", "test"]:
            print_summary(sp, sums[sp])
        print_summary("full_2y", summarize(trades, "full_2y", int(mask.sum()), period_days=period_days_from_panel(panel, 0, len(panel))))
    return variants

# ==============================================================================
# ML HELPERS
# ==============================================================================

def ensure_optuna() -> bool:
    try:
        import optuna  # noqa
        return True
    except Exception:
        return False


def ensure_xgboost() -> bool:
    try:
        import xgboost  # noqa
        return True
    except Exception:
        return False


def split_name_for_signal_i(i: int, splits: Dict[str, SplitDef]) -> str:
    for name, sp in splits.items():
        if sp.start <= int(i) < sp.end:
            return name
    return "unknown"


def max_drawdown_positive(pnls: np.ndarray) -> float:
    pnls = np.asarray(pnls, dtype=float)
    if pnls.size == 0:
        return 0.0
    eq = np.cumsum(pnls); peak = np.maximum.accumulate(eq); dd = peak - eq
    return float(np.max(dd)) if dd.size else 0.0


def profit_factor_array(pnls: np.ndarray) -> float:
    pnls = np.asarray(pnls, dtype=float)
    wins = pnls[pnls > 0].sum(); losses = -pnls[pnls < 0].sum()
    if losses == 0 and wins > 0:
        return float("inf")
    if losses == 0:
        return 0.0
    return float(wins / losses)


def count_days_from_times(times: pd.Series) -> int:
    if times is None or len(times) == 0:
        return 0
    ts = pd.to_datetime(times, errors="coerce").dropna()
    return int(ts.dt.floor("D").nunique()) if len(ts) else 0


def side_min_taken(side: str) -> int:
    return int(ML["MIN_TAKEN_LONG"] if side.upper() == "LONG" else ML["MIN_TAKEN_SHORT"])


def side_min_coverage(side: str) -> float:
    return float(ML["LIGHT_VETO_MIN_COVERAGE_LONG"] if side.upper() == "LONG" else ML["LIGHT_VETO_MIN_COVERAGE_SHORT"])


def side_target_coverage(side: str) -> float:
    return float(ML["LIGHT_VETO_TARGET_COVERAGE_LONG"] if side.upper() == "LONG" else ML["LIGHT_VETO_TARGET_COVERAGE_SHORT"])


def side_max_veto_rate(side: str) -> float:
    return float(ML["MAX_VETO_RATE_LONG"] if side.upper() == "LONG" else ML["MAX_VETO_RATE_SHORT"])


def ml_profit_score(m: Dict[str, Any]) -> float:
    pf_term = 0.0 if not np.isfinite(m["pf"]) else max(0.0, min(float(m["pf"]), 4.0) - 1.0)
    cov_term = -abs(float(m.get("coverage", 0.0)) - float(m.get("target_cov", 0.0)))
    return float(
        ML["SCORE_NET_W"] * m["net"]
        + ML["SCORE_AVGNET_W"] * m["avgNet"]
        + ML["SCORE_PF_W"] * pf_term
        + ML["SCORE_WR_W"] * m["wr"]
        - ML["SCORE_DD_W"] * m["maxDD"]
        + ML["SCORE_TRADE_W"] * math.sqrt(max(0, m["taken"]))
        + ML["SCORE_COVERAGE_W"] * cov_term
        + ML["SCORE_VETO_BADNESS_W"] * float(m.get("veto_badness", 0.0))
        - ML["SCORE_EXCESS_VETO_PENALTY_W"] * float(m.get("excess_veto", 0.0))
    )


def ml_perf(pnls: np.ndarray, total_count: int, taken_count: int, times: Optional[pd.Series] = None, target_cov: float = 1.0, veto_badness: float = 0.0, excess_veto: float = 0.0, period_days: Optional[int] = None) -> Dict[str, Any]:
    pnls = np.asarray(pnls, dtype=float)
    net = float(pnls.sum()) if pnls.size else 0.0
    dd = max_drawdown_positive(pnls)
    pf = profit_factor_array(pnls)
    wr = float((pnls > 0).mean()) if pnls.size else 0.0
    cov = float(taken_count / total_count) if total_count > 0 else 0.0
    days = int(period_days) if period_days is not None else (count_days_from_times(times) if times is not None else 0)
    tpd = float(taken_count / days) if days > 0 else 0.0
    out = {"net": net, "maxDD": dd, "pf": float(pf), "wr": wr, "coverage": cov, "taken": int(taken_count), "total": int(total_count), "days": days, "tpd": tpd, "avgNet": float(pnls.mean()) if pnls.size else 0.0, "target_cov": target_cov, "veto_badness": veto_badness, "excess_veto": excess_veto}
    out["score"] = ml_profit_score(out)
    return out


def threshold_grid() -> np.ndarray:
    return np.linspace(float(ML["THR_MIN"]), float(ML["THR_MAX"]), int(ML["THR_STEPS"]))


def choose_threshold_conservative_light(side: str, p: np.ndarray, pnl: np.ndarray, times: pd.Series) -> Optional[Dict[str, Any]]:
    p = np.asarray(p, dtype=float); pnl = np.asarray(pnl, dtype=float)
    total = len(p)
    if total == 0:
        return None
    rule_dd = max_drawdown_positive(pnl)
    dd_cap = max(float(ML["LOCAL_DD_CAP_MIN"]), float(ML["LOCAL_DD_CAP_FRAC"]) * rule_dd)
    min_cov = side_min_coverage(side)
    target_cov = side_target_coverage(side)
    max_veto = side_max_veto_rate(side)
    min_taken = side_min_taken(side)
    candidates = []
    for thr in threshold_grid():
        take = p >= float(thr)
        veto = ~take
        taken = int(take.sum())
        if taken < min_taken:
            continue
        cov = float(taken / total)
        veto_rate = 1.0 - cov
        if veto_rate > max_veto:
            continue
        veto_pnl = pnl[veto]
        veto_badness = float(-np.mean(veto_pnl)) if veto_pnl.size and np.mean(veto_pnl) < 0 else 0.0
        excess_veto = max(0.0, veto_rate - max_veto)
        m = ml_perf(pnl[take], total_count=total, taken_count=taken, times=times.reset_index(drop=True)[take], target_cov=target_cov, veto_badness=veto_badness, excess_veto=excess_veto)
        candidates.append({"thr": float(thr), "score": m["score"], "net": m["net"], "avgNet": m["avgNet"], "maxDD": m["maxDD"], "pf": m["pf"], "wr": m["wr"], "cov": m["coverage"], "taken": m["taken"], "tpd": m["tpd"], "veto_rate": veto_rate, "veto_badness": veto_badness})
    if not candidates:
        return None
    feasible = [r for r in candidates if r["cov"] >= min_cov and r["maxDD"] <= dd_cap]
    if not feasible:
        feasible = [r for r in candidates if r["cov"] >= min_cov]
    if not feasible:
        feasible = candidates
    return sorted(feasible, key=lambda r: (-r["score"], -r["cov"], -r["net"], r["maxDD"]))[0]


class IdentityCalibrator:
    def __init__(self, estimator):
        self.estimator = estimator
    def predict_proba(self, X):
        p = np.clip(self.estimator.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
        return np.column_stack([1.0 - p, p])


def calibrate_prefit(estimator, X_cal, y_cal, method: str = "sigmoid"):
    if method == "none":
        return IdentityCalibrator(estimator)
    from sklearn.calibration import CalibratedClassifierCV
    try:
        from sklearn.frozen import FrozenEstimator
        cal = CalibratedClassifierCV(estimator=FrozenEstimator(estimator), method=method, cv=None)
    except Exception:
        cal = CalibratedClassifierCV(estimator=estimator, method=method, cv="prefit")
    cal.fit(X_cal, y_cal)
    return cal


def expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
    y_true = np.asarray(y_true, dtype=float); y_prob = np.asarray(y_prob, dtype=float)
    if y_true.size == 0 or y_prob.size == 0:
        return np.nan
    if np.allclose(y_prob, y_prob[0]):
        return float(abs(np.mean(y_true) - y_prob[0]))
    edges = np.unique(np.quantile(y_prob, np.linspace(0, 1, n_bins + 1)))
    if len(edges) < 2:
        return float(abs(np.mean(y_true) - np.mean(y_prob)))
    bins = np.digitize(y_prob, edges[1:-1], right=True)
    ece = 0.0
    for b in range(len(edges) - 1):
        mask = bins == b
        if np.any(mask):
            ece += float(np.mean(mask)) * abs(float(np.mean(y_true[mask])) - float(np.mean(y_prob[mask])))
    return float(ece)


def build_monotone_constraints(feature_cols: List[str]) -> str:
    mono = []
    for c in feature_cols:
        lc = c.lower()
        mono.append(-1 if lc in ["spread", "entry_gap_minutes"] or lc.startswith("age_") else 0)
    return "(" + ",".join(str(x) for x in mono) + ")"


def build_xgb_from_trial(trial, feature_cols: List[str]):
    from xgboost import XGBClassifier
    booster = trial.suggest_categorical("booster", ["gbtree", "dart"])
    grow_policy = trial.suggest_categorical("grow_policy", ["depthwise", "lossguide"])
    params = {
        "booster": booster,
        "grow_policy": grow_policy,
        "n_estimators": trial.suggest_int("n_estimators", 180, 850),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.16, log=True),
        "max_depth": trial.suggest_int("max_depth", 2, 5),
        "min_child_weight": trial.suggest_float("min_child_weight", 0.6, 14.0, log=True),
        "gamma": trial.suggest_float("gamma", 0.0, 6.0),
        "subsample": trial.suggest_float("subsample", 0.60, 0.95),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.55, 0.95),
        "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 8.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 12.0, log=True),
        "scale_pos_weight": trial.suggest_float("scale_pos_weight", 0.5, 3.5),
        "tree_method": "hist",
        "max_bin": 256,
        "monotone_constraints": build_monotone_constraints(feature_cols),
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "n_jobs": 1,
        "random_state": 42,
    }
    if grow_policy == "lossguide":
        params["max_leaves"] = trial.suggest_int("max_leaves", 16, 160)
    if booster == "dart":
        params["sample_type"] = trial.suggest_categorical("sample_type", ["uniform", "weighted"])
        params["normalize_type"] = trial.suggest_categorical("normalize_type", ["tree", "forest"])
        params["rate_drop"] = trial.suggest_float("rate_drop", 0.0, 0.25)
        params["skip_drop"] = trial.suggest_float("skip_drop", 0.0, 0.25)
    return XGBClassifier(**params)


def make_xgb_from_params(params: Dict[str, Any], feature_cols: List[str]):
    from xgboost import XGBClassifier
    p = dict(params)
    p["tree_method"] = p.get("tree_method", "hist")
    p["max_bin"] = p.get("max_bin", 256)
    p["monotone_constraints"] = build_monotone_constraints(feature_cols)
    p["objective"] = "binary:logistic"
    p["eval_metric"] = "logloss"
    p["n_jobs"] = 1
    p["random_state"] = 42
    return XGBClassifier(**p)


def pipeline_for_model(model):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    return Pipeline([("imp", SimpleImputer(strategy="median")), ("clf", model)])


def allowed_snapshot_column(col: str) -> bool:
    c = str(col); cl = c.lower()
    if is_leaky_col(cl):
        return False
    if c in {"ts_open", "ts_close", "entry_time_next", "entry_ts_next", "entry_open_next", "next_index", "valid_next_entry"}:
        return False
    if cl.startswith("ts_") or cl.endswith("__ts_close") or cl.endswith("_ts_close"):
        return False
    if "time_next" in cl or "open_next" in cl or "next_index" in cl:
        return False
    return True


def ml_label_floor(panel: pd.DataFrame, trade_row: pd.Series) -> float:
    sig_i = int(trade_row["signal_i"]); entry = float(trade_row["entry"]); side = str(trade_row["side"]).upper()
    atr_abs = get_atr_abs(panel, sig_i, entry)
    if side == "LONG":
        min_net = float(ML["LABEL_MIN_NET_LONG"]); atr_mult = float(ML["LABEL_ATR_MULT_LONG"])
    else:
        min_net = float(ML["LABEL_MIN_NET_SHORT"]); atr_mult = float(ML["LABEL_ATR_MULT_SHORT"])
    atr_part = (atr_abs / max(entry, 1e-12)) * atr_mult
    setup_txt = str(trade_row.get("rule_trigger", "")).upper() + " " + str(trade_row.get("rule_setup", "")).upper()
    extra = float(ML["LABEL_BREAK_EXTRA"]) if "BREAK" in setup_txt else 0.0
    return float(max(min_net, atr_part + extra))


def build_trade_level_ml_dataset(panel: pd.DataFrame, raw_trades: pd.DataFrame, splits: Dict[str, SplitDef], feature_cols: Optional[List[str]] = None) -> Tuple[pd.DataFrame, List[str]]:
    if raw_trades.empty:
        return pd.DataFrame(), [] if feature_cols is None else feature_cols
    if feature_cols is None:
        numeric_cols = [c for c in panel.columns if allowed_snapshot_column(c) and pd.api.types.is_numeric_dtype(panel[c])]
    else:
        numeric_cols = list(feature_cols)
    rows = []
    for _, tr in raw_trades.iterrows():
        sig_i = int(tr["signal_i"])
        snap = panel.iloc[sig_i]
        row: Dict[str, Any] = {}
        for c in numeric_cols:
            row[c] = snap[c] if c in panel.columns else np.nan
        et = pd.Timestamp(tr["entry_time"])
        row["ml_hour"] = int(et.hour)
        row["ml_dow"] = int(et.dayofweek)
        row["ml_is_us_session"] = 1.0 if 13 <= int(et.hour) < 22 else 0.0
        row["ml_is_london_session"] = 1.0 if 7 <= int(et.hour) < 13 else 0.0
        row["ml_is_asia_session"] = 1.0 if 0 <= int(et.hour) < 7 else 0.0
        for c in ["signal_i", "entry_i", "exit_i", "signal_time", "entry_time", "exit_time", "engine", "side", "rule_source", "rule_setup", "rule_trigger", "rule_filter", "rule_exit", "net", "gross", "variant"]:
            row[c] = tr.get(c, np.nan)
        row["side"] = str(row["side"]).upper()
        row["engine"] = str(row["engine"]).upper()
        row["split"] = split_name_for_signal_i(int(row["signal_i"]), splits)
        row["label_floor"] = ml_label_floor(panel, tr)
        row["y"] = int(float(tr["net"]) >= row["label_floor"])
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("signal_i").reset_index(drop=True)
    banned = {"signal_i", "entry_i", "exit_i", "signal_time", "entry_time", "exit_time", "engine", "side", "rule_source", "rule_setup", "rule_trigger", "rule_filter", "rule_exit", "net", "gross", "label_floor", "y", "split", "variant"}
    if feature_cols is None:
        base_feature_cols = [c for c in df.columns if c not in banned and pd.api.types.is_numeric_dtype(df[c]) and not is_leaky_col(c)]
    else:
        base_feature_cols = list(feature_cols)
    return df, base_feature_cols


def select_ml_features_train_only(side_df: pd.DataFrame, feature_cols: List[str], side: str) -> List[str]:
    train = side_df[side_df["split"] == "train"].copy()
    y = train["y"].astype(float)
    pnl = train["net"].astype(float)
    scored = []
    for c in feature_cols:
        s = pd.to_numeric(train[c], errors="coerce") if c in train.columns else pd.Series(np.nan, index=train.index)
        valid = s.notna() & y.notna() & pnl.notna()
        if valid.sum() < max(20, int(0.08 * len(train))):
            continue
        if float(s[valid].std()) <= 1e-12:
            continue
        try:
            corr_y = abs(float(np.corrcoef(s[valid].rank(), y[valid])[0, 1]))
            corr_pnl = abs(float(np.corrcoef(s[valid].rank(), pnl[valid])[0, 1]))
            if not np.isfinite(corr_y): corr_y = 0.0
            if not np.isfinite(corr_pnl): corr_pnl = 0.0
        except Exception:
            corr_y = 0.0; corr_pnl = 0.0
        non_na = float(valid.mean())
        score = 0.65 * corr_y + 0.35 * corr_pnl + 0.01 * non_na
        scored.append((score, corr_y, corr_pnl, non_na, c))
    must_keep = [c for c in ["ml_hour", "ml_dow", "ml_is_us_session", "ml_is_london_session", "ml_is_asia_session"] if c in feature_cols]
    scored = sorted(scored, key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    selected = []
    for c in must_keep:
        if c not in selected:
            selected.append(c)
    for _, _, _, _, c in scored:
        if c not in selected:
            selected.append(c)
        if len(selected) >= int(ML["MAX_SNAPSHOT_FEATURES"]):
            break
    print(f"{side}_train_only_selected_features={len(selected):,}")
    return selected


def purge_before_boundary(df: pd.DataFrame, boundary_signal_i: int, cap_recent: bool = False) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    b = int(boundary_signal_i); embargo = int(ML["EMBARGO_BARS"])
    out = df[(df["exit_i"].astype(int) < b) & (df["signal_i"].astype(int) < b - embargo)].copy().sort_values("signal_i").reset_index(drop=True)
    if cap_recent and len(out) > int(ML["RECENT_TRAIN_MAX"]):
        out = out.iloc[-int(ML["RECENT_TRAIN_MAX"]):].reset_index(drop=True)
    return out


def make_wf_folds_side(side_df: pd.DataFrame, side: str) -> List[Dict[str, pd.DataFrame]]:
    df = side_df.sort_values("signal_i").reset_index(drop=True)
    n = len(df); folds = []
    if n < 120:
        return folds
    eva_len = max(20 if side.upper() == "LONG" else 45, int(0.14 * n))
    hist_min = max(70 if side.upper() == "LONG" else 160, int(0.42 * n))
    max_start = n - eva_len
    if max_start <= hist_min:
        return folds
    starts = np.linspace(hist_min, max_start, int(ML["N_WF_FOLDS"]), dtype=int)
    used = set()
    for st in starts:
        st = int(st)
        if st in used:
            continue
        used.add(st)
        eva = df.iloc[st: st + eva_len].copy().reset_index(drop=True)
        boundary = int(eva["signal_i"].min())
        hist = purge_before_boundary(df.iloc[:st].copy(), boundary, cap_recent=False)
        if len(hist) < (60 if side.upper() == "LONG" else 140) or len(eva) < (15 if side.upper() == "LONG" else 30):
            continue
        cut1 = int(len(hist) * 0.62); cut2 = int(len(hist) * 0.81)
        train = hist.iloc[:cut1].copy()
        cal = hist.iloc[cut1:cut2].copy()
        thr = hist.iloc[cut2:].copy()
        train = train.iloc[-int(ML["RECENT_TRAIN_MAX"]):].reset_index(drop=True)
        if len(train) < (45 if side.upper() == "LONG" else 100) or len(cal) < int(ML["CALIB_MIN_ROWS"]) or len(thr) < int(ML["CALIB_MIN_ROWS"]):
            continue
        if len(np.unique(train["y"].astype(int))) < 2:
            continue
        folds.append({"train": train.reset_index(drop=True), "cal": cal.reset_index(drop=True), "thr": thr.reset_index(drop=True), "eva": eva})
    return folds


def choose_best_calibration_and_threshold(side: str, estimator, cal_df: pd.DataFrame, val_df: pd.DataFrame, feature_cols: List[str]) -> Optional[Dict[str, Any]]:
    from sklearn.metrics import brier_score_loss, roc_auc_score
    if len(cal_df) < int(ML["CALIB_MIN_ROWS"]) or len(val_df) < side_min_taken(side):
        return None
    X_cal = cal_df[feature_cols]; y_cal = cal_df["y"].astype(int).values
    X_val = val_df[feature_cols]; y_val = val_df["y"].astype(int).values
    pnl_val = val_df["net"].astype(float).values
    times_val = pd.to_datetime(val_df["entry_time"], errors="coerce")
    best = None
    for method in CALIBRATION_METHODS:
        try:
            cal_model = calibrate_prefit(estimator, X_cal, y_cal, method=method)
            p_val = np.clip(cal_model.predict_proba(X_val)[:, 1], 1e-6, 1 - 1e-6)
            thr_info = choose_threshold_conservative_light(side, p_val, pnl_val, times_val)
            if thr_info is None:
                continue
            base_thr = float(thr_info["thr"])
            take = p_val >= base_thr
            veto = ~take
            veto_pnl = pnl_val[veto]
            veto_badness = float(-np.mean(veto_pnl)) if veto_pnl.size and np.mean(veto_pnl) < 0 else 0.0
            metrics = ml_perf(pnl_val[take], total_count=len(val_df), taken_count=int(take.sum()), times=times_val.reset_index(drop=True)[take], target_cov=side_target_coverage(side), veto_badness=veto_badness)
            if metrics["taken"] < side_min_taken(side):
                continue
            brier = brier_score_loss(y_val, p_val)
            ece = expected_calibration_error(y_val, p_val)
            auc = roc_auc_score(y_val, p_val) if len(np.unique(y_val)) > 1 else np.nan
            key = (metrics["score"], metrics["net"], metrics["avgNet"], -metrics["maxDD"], metrics["coverage"], -ece, -brier)
            cand = {"method": method, "model": cal_model, "base_thr": base_thr, "thr_info": thr_info, "metrics": metrics, "brier": float(brier), "ece": float(ece), "auc": float(auc) if not pd.isna(auc) else np.nan, "key": key}
            if best is None or key > best["key"]:
                best = cand
        except Exception as e:
            print(f"calibration_skipped side={side} method={method} error={type(e).__name__}: {e}")
    return best


def optuna_tune_xgb_side(side_df: pd.DataFrame, side: str, feature_cols: List[str]):
    import optuna
    folds = make_wf_folds_side(side_df, side)
    if not folds:
        raise RuntimeError(f"No valid ML walk-forward folds for {side} after purge/embargo.")
    print(f"{side}_ML_WF_FOLDS={len(folds)}")
    def objective(trial):
        model = build_xgb_from_trial(trial, feature_cols)
        pipe = pipeline_for_model(model)
        fold_scores = []
        used_thrs = []
        for k, fd in enumerate(folds):
            train = fd["train"]; cal = fd["cal"]; thr = fd["thr"]; eva = fd["eva"]
            if len(np.unique(train["y"].astype(int))) < 2:
                fold_scores.append(-1e9); continue
            pipe.fit(train[feature_cols], train["y"].astype(int).values)
            best_cal = choose_best_calibration_and_threshold(side, pipe, cal, thr, feature_cols)
            if best_cal is None:
                fold_scores.append(-1e9); continue
            base_thr = float(best_cal["base_thr"])
            used_thrs.append(base_thr)
            p_eva = np.clip(best_cal["model"].predict_proba(eva[feature_cols])[:, 1], 1e-6, 1 - 1e-6)
            take = p_eva >= base_thr
            veto = ~take
            pnl_eva = eva["net"].astype(float).values
            veto_pnl = pnl_eva[veto]
            veto_badness = float(-np.mean(veto_pnl)) if veto_pnl.size and np.mean(veto_pnl) < 0 else 0.0
            m = ml_perf(pnl_eva[take], total_count=len(eva), taken_count=int(take.sum()), times=pd.to_datetime(eva["entry_time"], errors="coerce").reset_index(drop=True)[take], target_cov=side_target_coverage(side), veto_badness=veto_badness)
            score = m["score"] if m["taken"] >= side_min_taken(side) and m["coverage"] >= side_min_coverage(side) else -1e9
            fold_scores.append(float(score))
            trial.report(float(np.mean(fold_scores)), k)
            if trial.should_prune():
                raise optuna.TrialPruned()
        if used_thrs:
            trial.set_user_attr("wf_thr_seq", [float(x) for x in used_thrs])
            trial.set_user_attr("wf_thr_median", float(np.median(used_thrs)))
        return float(np.mean(fold_scores))
    sampler = optuna.samplers.TPESampler(seed=42)
    pruner = optuna.pruners.MedianPruner(n_startup_trials=max(8, int(ML["N_TRIALS"] // 3)))
    study = optuna.create_study(direction="maximize", sampler=sampler, pruner=pruner)
    study.optimize(objective, n_trials=int(ML["N_TRIALS"]), show_progress_bar=False)
    return study


def train_final_ml_side(side_df: pd.DataFrame, side: str, feature_cols: List[str], best_params: Dict[str, Any], split_days: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    from sklearn.metrics import roc_auc_score, brier_score_loss
    train_all = side_df[side_df["split"] == "train"].sort_values("signal_i").reset_index(drop=True)
    val = side_df[side_df["split"] == "validation"].sort_values("signal_i").reset_index(drop=True)
    test = side_df[side_df["split"] == "test"].sort_values("signal_i").reset_index(drop=True)
    if len(train_all) < 50 or len(val) < side_min_taken(side) or len(test) == 0:
        die(f"{side} ML dataset too small for mandatory final train/validation/test.")
    cut = int(len(train_all) * (1.0 - float(ML["CALIB_FIT_PCT"])))
    min_cal = max(int(ML["CALIB_MIN_ROWS"]), 20 if side == "LONG" else 40)
    cut = max(35, min(cut, len(train_all) - min_cal))
    train_fit = train_all.iloc[:cut].copy()
    cal_fit = train_all.iloc[cut:].copy()
    train_fit = train_fit.iloc[-int(ML["RECENT_TRAIN_MAX"]):].reset_index(drop=True)
    cal_fit = cal_fit.reset_index(drop=True)
    if len(np.unique(train_fit["y"].astype(int))) < 2:
        die(f"{side} ML train_fit has one class only.")
    model = make_xgb_from_params(best_params, feature_cols)
    pipe = pipeline_for_model(model)
    pipe.fit(train_fit[feature_cols], train_fit["y"].astype(int).values)
    best_cal = choose_best_calibration_and_threshold(side, pipe, cal_fit, val, feature_cols)
    if best_cal is None:
        die(f"{side} final mandatory ML calibration/threshold selection failed.")
    cal_model = best_cal["model"]
    base_thr = float(best_cal["base_thr"])
    method = str(best_cal["method"])
    def eval_part(part: pd.DataFrame, period_days: Optional[int] = None):
        p = np.clip(cal_model.predict_proba(part[feature_cols])[:, 1], 1e-6, 1 - 1e-6)
        take = p >= base_thr
        veto = ~take
        pnl = part["net"].astype(float).values
        veto_pnl = pnl[veto]
        veto_badness = float(-np.mean(veto_pnl)) if veto_pnl.size and np.mean(veto_pnl) < 0 else 0.0
        m = ml_perf(pnl[take], total_count=len(part), taken_count=int(take.sum()), times=pd.to_datetime(part["entry_time"], errors="coerce").reset_index(drop=True)[take], target_cov=side_target_coverage(side), veto_badness=veto_badness, period_days=period_days)
        return m, p, take
    raw = {}; light = {}; probs = {}; takes = {}
    for name, part in [("train", train_all), ("validation", val), ("test", test), ("full_2y", side_df)]:
        times = pd.to_datetime(part["entry_time"], errors="coerce")
        days_for_split = split_days.get(name) if split_days is not None else None
        raw[name] = ml_perf(part["net"].astype(float).values, total_count=len(part), taken_count=len(part), times=times, target_cov=1.0, period_days=days_for_split)
        light[name], probs[name], takes[name] = eval_part(part, period_days=days_for_split)
    if light["validation"]["coverage"] < side_min_coverage(side):
        die(f"{side} mandatory conservative ML selected coverage below minimum: {light['validation']['coverage']:.3f} < {side_min_coverage(side):.3f}")
    y_val = val["y"].astype(int).values; p_val = probs["validation"]
    y_test = test["y"].astype(int).values; p_test = probs["test"]
    auc_val = roc_auc_score(y_val, p_val) if len(np.unique(y_val)) > 1 else np.nan
    auc_test = roc_auc_score(y_test, p_test) if len(np.unique(y_test)) > 1 else np.nan
    brier_val = brier_score_loss(y_val, p_val); brier_test = brier_score_loss(y_test, p_test)
    ece_val = expected_calibration_error(y_val, p_val); ece_test = expected_calibration_error(y_test, p_test)
    side_out = side_df.copy().reset_index(drop=True)
    p_all = np.clip(cal_model.predict_proba(side_out[feature_cols])[:, 1], 1e-6, 1 - 1e-6)
    take_all = p_all >= base_thr
    side_out["ml_prob"] = p_all
    side_out["ml_take"] = take_all
    side_out["ml_threshold"] = base_thr
    side_out["ml_model_side"] = side
    section(f"MANDATORY CONSERVATIVE ML LIGHT FILTER — {side} FINAL VALIDATION-SELECTED RESULT")
    print(f"SIDE={side}")
    print(f"CALIBRATION_METHOD={method}")
    print(f"ML_THRESHOLD_{side}={base_thr:.3f}")
    print(f"MIN_COVERAGE_{side}={side_min_coverage(side):.2f} TARGET_COVERAGE_{side}={side_target_coverage(side):.2f} MAX_VETO_RATE_{side}={side_max_veto_rate(side):.2f}")
    print(f"AUC_VAL={auc_val:.4f} AUC_TEST_AUDIT={auc_test:.4f} BRIER_VAL={brier_val:.4f} BRIER_TEST_AUDIT={brier_test:.4f} ECE_VAL={ece_val:.4f} ECE_TEST_AUDIT={ece_test:.4f}")
    subsection(f"{side} RAW RULES vs {side} MANDATORY ML")
    print(f"{'scenario':<16} {'split':<11} {'trades':>8} {'coverage':>10} {'tpd':>8} {'WR':>8} {'PF':>8} {'net':>10} {'maxDD':>10} {'avgNet':>10}")
    for scenario, store in [("RAW_RULES", raw), ("ML_MANDATORY", light)]:
        for sp in ["train", "validation", "test", "full_2y"]:
            m = store[sp]
            print(f"{scenario:<16} {sp:<11} {m['taken']:>8,} {m['coverage']*100:>9.2f}% {m['tpd']:>8.3f} {m['wr']*100:>7.2f}% {fmt_pf(m['pf']):>8} {m['net']*100:>9.3f}% {m['maxDD']*100:>9.3f}% {m['avgNet']*10000:>9.2f}bp")
    return {"side": side, "model": cal_model, "threshold": base_thr, "calibration_method": method, "feature_cols": feature_cols, "best_params": best_params, "raw": raw, "light": light, "auc_val": auc_val, "auc_test": auc_test, "brier_val": brier_val, "brier_test": brier_test, "ece_val": ece_val, "ece_test": ece_test, "scored_trades": side_out}


def prepare_raw_trades_for_ml(long_variant: RuleVariant, short_variant: RuleVariant) -> pd.DataFrame:
    frames = []
    lt = long_variant.trades.copy()
    lt["rule_setup"] = LONG_SETUP_NAME
    lt["rule_trigger"] = LONG_TRIGGER
    lt["rule_filter"] = long_variant.filter_name
    lt["rule_exit"] = long_variant.cfg.name
    lt["rule_source"] = "LONG_CURRENT"
    lt["variant"] = "LONG_CURRENT"
    frames.append(lt)
    st = short_variant.trades.copy()
    st["rule_setup"] = SHORT_LOCKED_SETUP_NAME
    st["rule_trigger"] = SHORT_LOCKED_TRIGGER
    st["rule_filter"] = short_variant.filter_name
    st["rule_exit"] = short_variant.cfg.name
    st["rule_source"] = short_variant.name
    st["variant"] = short_variant.name
    frames.append(st)
    return pd.concat(frames, ignore_index=True).sort_values(["signal_i", "engine"]).reset_index(drop=True)


def train_side_model(panel: pd.DataFrame, splits: Dict[str, SplitDef], raw_trades: pd.DataFrame, side: str, base_feature_cols: Optional[List[str]] = None, split_days: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    side_raw = raw_trades[raw_trades["side"] == side].copy().reset_index(drop=True)
    df_ml, all_feature_cols = build_trade_level_ml_dataset(panel, side_raw, splits, feature_cols=base_feature_cols)
    if df_ml.empty:
        die(f"No {side} ML rows.")
    section(f"MANDATORY CONSERVATIVE ML — {side} MODEL TRAINING")
    print(f"{side}_rows={len(df_ml):,}")
    for sp in ["train", "validation", "test"]:
        part = df_ml[df_ml["split"] == sp]
        if len(part):
            print(f"{side} {sp:<10} rows={len(part):,} label_rate={part['y'].mean()*100:.2f}% net={part['net'].sum()*100:.3f}%")
    feature_cols = select_ml_features_train_only(df_ml, all_feature_cols, side)
    print(f"ML_FEATURES_{side}_FINAL={len(feature_cols):,}")
    trainval = df_ml[df_ml["split"].isin(["train", "validation"])].copy().sort_values("signal_i").reset_index(drop=True)
    print(f"{side}_N_TRIALS={int(ML['N_TRIALS'])} {side}_N_WF_FOLDS={int(ML['N_WF_FOLDS'])}")
    print(f"{side}_MIN_COVERAGE={side_min_coverage(side):.2f} {side}_TARGET_COVERAGE={side_target_coverage(side):.2f} {side}_MAX_VETO_RATE={side_max_veto_rate(side):.2f}")
    study = optuna_tune_xgb_side(trainval, side, feature_cols)
    best_params = dict(study.best_trial.params)
    subsection(f"{side} ML BEST TRIAL")
    print(f"best_value={study.best_value:.6f}")
    print(f"wf_thr_seq={study.best_trial.user_attrs.get('wf_thr_seq', [])}")
    print(f"wf_thr_median={study.best_trial.user_attrs.get('wf_thr_median', None)}")
    print("best_params:")
    for k in sorted(best_params):
        print(f"  {k}: {best_params[k]}")
    return train_final_ml_side(df_ml, side, feature_cols, best_params, split_days=split_days)


def score_trades_with_side_model(panel: pd.DataFrame, splits: Dict[str, SplitDef], raw_trades: pd.DataFrame, model_info: Dict[str, Any]) -> pd.DataFrame:
    side = model_info["side"]
    df_ml, _ = build_trade_level_ml_dataset(panel, raw_trades[raw_trades["side"] == side].copy(), splits, feature_cols=model_info["feature_cols"])
    if df_ml.empty:
        return df_ml
    p = np.clip(model_info["model"].predict_proba(df_ml[model_info["feature_cols"]])[:, 1], 1e-6, 1 - 1e-6)
    df_ml["ml_prob"] = p
    df_ml["ml_take"] = p >= float(model_info["threshold"])
    df_ml["ml_threshold"] = float(model_info["threshold"])
    df_ml["ml_model_side"] = side
    return df_ml


def summarize_scored(scored: pd.DataFrame, split: str, period_days: Optional[int] = None) -> Dict[str, Any]:
    part = scored if split == "full_2y" else scored[scored["split"] == split]
    take = part["ml_take"].astype(bool).values if len(part) else np.array([], dtype=bool)
    pnl = part["net"].astype(float).values if len(part) else np.array([], dtype=float)
    times = pd.to_datetime(part["entry_time"], errors="coerce").reset_index(drop=True) if len(part) else pd.Series(dtype="datetime64[ns]")
    return ml_perf(pnl[take], total_count=len(part), taken_count=int(take.sum()), times=times[take] if len(times) else times, target_cov=1.0, period_days=period_days)


def global_side_priority(side: Any) -> int:
    side_s = str(side).upper()
    if GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY == "SHORT_FIRST":
        return 0 if side_s == "SHORT" else 1
    return 0 if side_s == "LONG" else 1


def apply_global_no_overlap_after_ml(long_scored: pd.DataFrame, short_scored: pd.DataFrame, label: str = "") -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Apply one-position chronological execution AFTER ML acceptance.

    This does not retrain ML and does not change rule exits.
    It only changes which ML-accepted trades are actually allowed into the final portfolio.

    Policy:
    - one open ETHUSDT position globally across LONG and SHORT;
    - no flip: opposite-side signals during an open trade are ignored;
    - next trade is allowed only when entry_i > previous_exit_i;
    - deterministic same-bar priority is controlled by GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY.
    """
    frames: List[pd.DataFrame] = []
    for name, df in [("LONG", long_scored), ("SHORT", short_scored)]:
        if isinstance(df, pd.DataFrame) and not df.empty:
            part = df.copy()
            part["__origin_side_for_no_overlap"] = name
            part["__origin_order_for_no_overlap"] = np.arange(len(part), dtype=int)
            frames.append(part)

    if not frames:
        return long_scored.copy(), short_scored.copy(), {
            "label": label,
            "global_no_overlap_enabled": bool(GLOBAL_NO_OVERLAP_AFTER_ML),
            "pre_ml_taken": 0,
            "post_no_overlap_taken": 0,
            "skipped_by_global_no_overlap": 0,
            "entry_overlap_count_after_filter": 0,
        }

    combined = pd.concat(frames, ignore_index=True, sort=False).copy()

    if "ml_take" not in combined.columns:
        combined["ml_take"] = True

    combined["ml_take_before_global_no_overlap"] = combined["ml_take"].astype(bool)
    combined["global_no_overlap_take"] = False
    combined["global_no_overlap_skipped"] = False
    combined["global_no_overlap_skip_reason"] = ""

    if not GLOBAL_NO_OVERLAP_AFTER_ML:
        combined["global_no_overlap_take"] = combined["ml_take_before_global_no_overlap"]
        combined["ml_take"] = combined["global_no_overlap_take"]
    else:
        need_cols = {"entry_i", "signal_i", "exit_i", "side"}
        missing = sorted([c for c in need_cols if c not in combined.columns])
        if missing:
            raise RuntimeError(f"Cannot apply global no-overlap; missing columns: {missing}")

        candidates = combined[combined["ml_take_before_global_no_overlap"]].copy()
        candidates["__entry_i_num"] = pd.to_numeric(candidates["entry_i"], errors="coerce")
        candidates["__signal_i_num"] = pd.to_numeric(candidates["signal_i"], errors="coerce")
        candidates["__exit_i_num"] = pd.to_numeric(candidates["exit_i"], errors="coerce")
        candidates["__side_priority"] = candidates["side"].map(global_side_priority)
        candidates = candidates.dropna(subset=["__entry_i_num", "__signal_i_num", "__exit_i_num"]).sort_values(
            ["__entry_i_num", "__signal_i_num", "__side_priority", "__origin_order_for_no_overlap"]
        )

        current_exit_i = -1
        accepted_index: List[int] = []
        skipped_index: List[int] = []
        for idx, row in candidates.iterrows():
            entry_i = int(row["__entry_i_num"])
            exit_i = int(row["__exit_i_num"])
            if entry_i > current_exit_i:
                accepted_index.append(idx)
                current_exit_i = max(current_exit_i, exit_i)
            else:
                skipped_index.append(idx)

        combined.loc[accepted_index, "global_no_overlap_take"] = True
        combined.loc[skipped_index, "global_no_overlap_skipped"] = True
        combined.loc[skipped_index, "global_no_overlap_skip_reason"] = "BLOCKED_BY_OPEN_POSITION"
        combined["ml_take"] = combined["global_no_overlap_take"]

    final_taken = combined[combined["global_no_overlap_take"]].copy()
    if not final_taken.empty:
        ordered = final_taken.sort_values(["entry_i", "signal_i", "side"]).reset_index(drop=True)
        entry_overlap_count = int((pd.to_numeric(ordered["entry_i"].iloc[1:], errors="coerce").to_numpy() <= pd.to_numeric(ordered["exit_i"].iloc[:-1], errors="coerce").to_numpy()).sum()) if len(ordered) > 1 else 0
        signal_overlap_count = int((pd.to_numeric(ordered["signal_i"].iloc[1:], errors="coerce").to_numpy() <= pd.to_numeric(ordered["exit_i"].iloc[:-1], errors="coerce").to_numpy()).sum()) if len(ordered) > 1 else 0
    else:
        entry_overlap_count = 0
        signal_overlap_count = 0

    audit = {
        "label": label,
        "global_no_overlap_enabled": bool(GLOBAL_NO_OVERLAP_AFTER_ML),
        "same_bar_priority": GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY,
        "flip_policy": "NO_FLIP_IGNORE_OPPOSITE_SIGNAL_WHILE_POSITION_OPEN" if NO_FLIP_ON_OPPOSITE_SIGNAL else "UNDEFINED",
        "pre_ml_taken": int(combined["ml_take_before_global_no_overlap"].sum()),
        "post_no_overlap_taken": int(combined["global_no_overlap_take"].sum()),
        "skipped_by_global_no_overlap": int(combined["global_no_overlap_skipped"].sum()),
        "entry_overlap_count_after_filter": int(entry_overlap_count),
        "signal_overlap_count_after_filter": int(signal_overlap_count),
    }

    long_clean = combined[combined["__origin_side_for_no_overlap"].eq("LONG")].copy().reset_index(drop=True)
    short_clean = combined[combined["__origin_side_for_no_overlap"].eq("SHORT")].copy().reset_index(drop=True)
    return long_clean, short_clean, audit


def print_global_no_overlap_audit(audit: Dict[str, Any]) -> None:
    subsection(f"GLOBAL NO-OVERLAP AFTER ML — {audit.get('label', '')}")
    print(f"enabled={audit.get('global_no_overlap_enabled')}")
    print(f"same_bar_priority={audit.get('same_bar_priority')}")
    print(f"flip_policy={audit.get('flip_policy')}")
    print(f"pre_ml_taken={audit.get('pre_ml_taken'):,}")
    print(f"post_no_overlap_taken={audit.get('post_no_overlap_taken'):,}")
    print(f"skipped_by_global_no_overlap={audit.get('skipped_by_global_no_overlap'):,}")
    print(f"entry_overlap_count_after_filter={audit.get('entry_overlap_count_after_filter')}")
    print(f"signal_overlap_count_after_filter={audit.get('signal_overlap_count_after_filter')}")


def prepare_combined_for_global_threshold_search(long_scored: pd.DataFrame, short_scored: pd.DataFrame) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for name, df in [("LONG", long_scored), ("SHORT", short_scored)]:
        if isinstance(df, pd.DataFrame) and not df.empty:
            part = df.copy()
            part["__origin_side_for_no_overlap"] = name
            part["__origin_order_for_no_overlap"] = np.arange(len(part), dtype=int)
            frames.append(part)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False).copy()
    need = {"side", "ml_prob", "entry_i", "signal_i", "exit_i", "net", "split", "entry_time"}
    missing = sorted([c for c in need if c not in combined.columns])
    if missing:
        raise RuntimeError(f"Cannot run global threshold search; missing columns: {missing}")
    combined["__side_priority"] = combined["side"].map(global_side_priority)
    combined["__entry_i_num"] = pd.to_numeric(combined["entry_i"], errors="coerce")
    combined["__signal_i_num"] = pd.to_numeric(combined["signal_i"], errors="coerce")
    combined["__exit_i_num"] = pd.to_numeric(combined["exit_i"], errors="coerce")
    combined["__net_num"] = pd.to_numeric(combined["net"], errors="coerce").fillna(0.0)
    combined = combined.dropna(subset=["__entry_i_num", "__signal_i_num", "__exit_i_num"]).sort_values(
        ["__entry_i_num", "__signal_i_num", "__side_priority", "__origin_order_for_no_overlap"]
    ).reset_index(drop=True)
    return combined


def simulate_global_no_overlap_acceptance(combined: pd.DataFrame, long_thr: float, short_thr: float) -> np.ndarray:
    if combined.empty:
        return np.array([], dtype=bool)
    side = combined["side"].astype(str).str.upper().to_numpy()
    prob = pd.to_numeric(combined["ml_prob"], errors="coerce").fillna(-np.inf).to_numpy(dtype=float)
    candidate = ((side == "LONG") & (prob >= float(long_thr))) | ((side == "SHORT") & (prob >= float(short_thr)))
    entry_i = combined["__entry_i_num"].to_numpy(dtype=int)
    exit_i = combined["__exit_i_num"].to_numpy(dtype=int)
    accepted = np.zeros(len(combined), dtype=bool)
    current_exit_i = -1
    for pos in range(len(combined)):
        if not candidate[pos]:
            continue
        if int(entry_i[pos]) > int(current_exit_i):
            accepted[pos] = True
            current_exit_i = max(int(current_exit_i), int(exit_i[pos]))
    return accepted


def summarize_acceptance_mask(combined: pd.DataFrame, accepted: np.ndarray, split_days: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    split_arr = combined["split"].astype(str).to_numpy() if not combined.empty else np.array([], dtype=str)
    net_arr = combined["__net_num"].to_numpy(dtype=float) if not combined.empty else np.array([], dtype=float)
    times = pd.to_datetime(combined["entry_time"], errors="coerce").reset_index(drop=True) if not combined.empty else pd.Series(dtype="datetime64[ns]")
    for sp in ["train", "validation", "test", "full_2y"]:
        part_mask = np.ones(len(combined), dtype=bool) if sp == "full_2y" else (split_arr == sp)
        take_mask = part_mask & accepted
        total_count = int(part_mask.sum())
        taken_count = int(take_mask.sum())
        out[sp] = ml_perf(
            net_arr[take_mask],
            total_count=total_count,
            taken_count=taken_count,
            times=times[take_mask] if len(times) else times,
            target_cov=1.0,
            period_days=split_days.get(sp),
        )
    return out


def threshold_pair_key(sums: Dict[str, Dict[str, Any]], long_thr: float, short_thr: float) -> Tuple[Any, ...]:
    tr = sums["train"]
    va = sums["validation"]
    # Test is intentionally not used. No forced TPD target is used.
    valid = int(tr["net"] > 0 and va["net"] > 0 and va["pf"] >= 1.0 and va["taken"] >= 50 and tr["taken"] >= 100)
    return (
        valid,
        va["net"],
        va["pf"],
        tr["net"],
        va["avgNet"],
        -va["maxDD"],
        tr["pf"],
        -abs(float(long_thr) - float(short_thr)),
    )


def choose_thresholds_inside_global_no_overlap(
    long_scored: pd.DataFrame,
    short_scored: pd.DataFrame,
    initial_long_thr: float,
    initial_short_thr: float,
    split_days: Dict[str, int],
    label: str,
) -> Tuple[float, float, pd.DataFrame, pd.DataFrame, Dict[str, Any], Dict[str, Dict[str, Any]]]:
    section(f"GLOBAL NO-OVERLAP THRESHOLD SELECTION — {label}")
    print("Threshold pair is selected on train+validation only inside the same global one-position simulator.")
    print("No forced target TPD is used. Test remains audit only.")
    combined = prepare_combined_for_global_threshold_search(long_scored, short_scored)
    if combined.empty:
        raise RuntimeError("Cannot select global thresholds; combined scored trades are empty.")

    best_key: Optional[Tuple[Any, ...]] = None
    best: Optional[Dict[str, Any]] = None
    grids = list(float(x) for x in threshold_grid())
    for long_thr in grids:
        for short_thr in grids:
            accepted = simulate_global_no_overlap_acceptance(combined, long_thr, short_thr)
            sums = summarize_acceptance_mask(combined, accepted, split_days)
            key = threshold_pair_key(sums, long_thr, short_thr)
            if best_key is None or key > best_key:
                best_key = key
                best = {"long_thr": float(long_thr), "short_thr": float(short_thr), "accepted": accepted, "sums": sums, "key": key}

    if best is None:
        raise RuntimeError("Global no-overlap threshold search failed.")

    selected_long_thr = float(best["long_thr"])
    selected_short_thr = float(best["short_thr"])
    print(f"INITIAL_LONG_THRESHOLD={float(initial_long_thr):.3f}")
    print(f"INITIAL_SHORT_THRESHOLD={float(initial_short_thr):.3f}")
    print(f"GLOBAL_SELECTED_LONG_THRESHOLD={selected_long_thr:.3f}")
    print(f"GLOBAL_SELECTED_SHORT_THRESHOLD={selected_short_thr:.3f}")

    long_adj = long_scored.copy()
    short_adj = short_scored.copy()
    if not long_adj.empty:
        long_adj["ml_threshold_before_global_selection"] = float(initial_long_thr)
        long_adj["ml_threshold"] = selected_long_thr
        long_adj["ml_take"] = pd.to_numeric(long_adj["ml_prob"], errors="coerce") >= selected_long_thr
    if not short_adj.empty:
        short_adj["ml_threshold_before_global_selection"] = float(initial_short_thr)
        short_adj["ml_threshold"] = selected_short_thr
        short_adj["ml_take"] = pd.to_numeric(short_adj["ml_prob"], errors="coerce") >= selected_short_thr

    long_clean, short_clean, audit = apply_global_no_overlap_after_ml(long_adj, short_adj, label=label)
    audit["initial_long_threshold"] = float(initial_long_thr)
    audit["initial_short_threshold"] = float(initial_short_thr)
    audit["global_selected_long_threshold"] = selected_long_thr
    audit["global_selected_short_threshold"] = selected_short_thr
    audit["threshold_selection_basis"] = "train+validation only; test audit only"
    audit["forced_target_tpd"] = "DISABLED"
    print_global_no_overlap_audit(audit)
    return selected_long_thr, selected_short_thr, long_clean, short_clean, audit, best["sums"]


def combined_variant_summary(long_scored: pd.DataFrame, short_scored: pd.DataFrame, split_days: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    combined = pd.concat([long_scored, short_scored], ignore_index=True).sort_values(["signal_i", "side"]).reset_index(drop=True)
    return {sp: summarize_scored(combined, sp, period_days=split_days.get(sp)) for sp in ["train", "validation", "test", "full_2y"]}


def print_combined_variant_table(rows: List[Dict[str, Any]]) -> None:
    print(f"{'variant':<20} {'train_trades':>13} {'val_trades':>11} {'test_trades':>12} {'full_trades':>12} {'full_tpd':>9} {'WR_full':>8} {'train_net':>11} {'val_net':>10} {'test_net':>10} {'full_net':>10} {'PF_full':>8} {'DD_full':>9} {'avgNet':>10}")
    for r in rows:
        print(f"{r['variant']:<20} {r['train_trades']:>13,} {r['val_trades']:>11,} {r['test_trades']:>12,} {r['full_trades']:>12,} {r['full_tpd']:>9.3f} {r.get('full_wr', np.nan)*100:>7.2f}% {r['train_net']*100:>10.3f}% {r['val_net']*100:>9.3f}% {r['test_net']*100:>9.3f}% {r['full_net']*100:>9.3f}% {fmt_pf(r['pf_full']):>8} {r['dd_full']*100:>8.3f}% {r['avgNet']*10000:>9.2f}bp")


def print_client_two_versions_report(rows: List[Dict[str, Any]]) -> None:
    rows_by_variant = {r["variant"]: r for r in rows}
    volume = rows_by_variant.get("SHORT_NO_FILTER")
    section("FINAL REPORT — VERSION B ONLY")
    print(f"Confirmation Mode: live artifacts/files will be saved to: {ML_ARTIFACT_DIR}")
    print(f"{'version':<30} {'short_variant':<18} {'profile':<36} {'trades':>10} {'tpd':>8} {'WR':>8} {'net':>10} {'PF':>8} {'DD':>9} {'avgNet':>10}")
    if volume is None:
        print("FINAL VERSION B — VOLUME missing: SHORT_NO_FILTER row was not found.")
        return
    print(f"{'FINAL VERSION B — VOLUME':<30} {'SHORT_NO_FILTER + ML':<18} {'confirmation / stronger ML':<36} {volume['full_trades']:>10,} {volume['full_tpd']:>8.3f} {volume.get('full_wr', np.nan)*100:>7.2f}% {volume['full_net']*100:>9.3f}% {fmt_pf(volume['pf_full']):>8} {volume['dd_full']*100:>8.3f}% {volume['avgNet']*10000:>9.2f}bp")


def select_final_variant(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Selection uses train + validation quality only. Test is not used for choosing.
    # No forced TPD target is applied. TPD is reported only.
    for r in rows:
        trainval_trades = r["train_trades"] + r["val_trades"]
        trainval_days = r.get("trainval_days", 1)
        r["trainval_tpd"] = trainval_trades / max(1, trainval_days)
    valid = [r for r in rows if r["train_net"] > 0 and r["val_net"] > 0 and r["pf_val"] >= 1.0]
    if valid:
        return sorted(valid, key=lambda r: (r["val_net"], r["pf_val"], r["train_net"], r["trainval_tpd"], -r["dd_val"]), reverse=True)[0]
    valid = [r for r in rows if r["train_net"] > 0 and r["val_net"] > 0]
    if valid:
        return sorted(valid, key=lambda r: (r["val_net"], r["pf_val"], r["train_net"], r["trainval_tpd"]), reverse=True)[0]
    return sorted(rows, key=lambda r: (r["val_net"], r["train_net"], r["pf_val"]), reverse=True)[0]


def run_mandatory_ml_comparison(panel: pd.DataFrame, splits: Dict[str, SplitDef], long_variant: RuleVariant, short_variants: Dict[str, RuleVariant]) -> Dict[str, Any]:
    section("MANDATORY ML — VERSION B ONLY")
    print("LONG ML is trained once with Version B run.")
    print("SHORT ML is trained only for Version B: SHORT_NO_FILTER.")
    print("Version B threshold pair selection is done inside global no-overlap simulator using train + validation only. Test is audit only.")
    if not ensure_xgboost():
        die("xgboost is not installed. Install xgboost in the same venv and rerun.")
    if not ensure_optuna():
        die("optuna is not installed. Install optuna in the same venv and rerun.")

    raw_for_long = prepare_raw_trades_for_ml(long_variant, short_variants["SHORT_NO_FILTER"])
    variant_results: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    split_days = build_period_days(panel, splits)
    n_trainval_days = split_days["train"] + split_days["validation"]
    long_model = train_side_model(panel, splits, raw_for_long[raw_for_long["side"] == "LONG"].copy(), "LONG", split_days=split_days)
    long_scored = score_trades_with_side_model(panel, splits, raw_for_long[raw_for_long["side"] == "LONG"].copy(), long_model)

    for variant_name in SHORT_EXPANSION_VARIANTS:
        section(f"MANDATORY ML FOR VERSION B: {variant_name}")
        raw_variant = prepare_raw_trades_for_ml(long_variant, short_variants[variant_name])
        short_model = train_side_model(panel, splits, raw_variant[raw_variant["side"] == "SHORT"].copy(), "SHORT", split_days=split_days)
        short_scored_raw = score_trades_with_side_model(panel, splits, raw_variant[raw_variant["side"] == "SHORT"].copy(), short_model)

        selected_long_thr, selected_short_thr, long_scored_for_variant, short_scored, no_overlap_audit, _threshold_selection_sums = choose_thresholds_inside_global_no_overlap(
            long_scored=long_scored,
            short_scored=short_scored_raw,
            initial_long_thr=float(long_model["threshold"]),
            initial_short_thr=float(short_model["threshold"]),
            split_days=split_days,
            label=variant_name,
        )
        long_model["threshold"] = float(selected_long_thr)
        short_model["threshold"] = float(selected_short_thr)
        sums = combined_variant_summary(long_scored_for_variant, short_scored, split_days)
        row = {
            "variant": variant_name,
            "train_trades": sums["train"]["taken"],
            "val_trades": sums["validation"]["taken"],
            "test_trades": sums["test"]["taken"],
            "full_trades": sums["full_2y"]["taken"],
            "full_tpd": sums["full_2y"]["tpd"],
            "train_net": sums["train"]["net"],
            "val_net": sums["validation"]["net"],
            "test_net": sums["test"]["net"],
            "full_net": sums["full_2y"]["net"],
            "pf_full": sums["full_2y"]["pf"],
            "pf_val": sums["validation"]["pf"],
            "pf_test": sums["test"]["pf"],
            "dd_full": sums["full_2y"]["maxDD"],
            "dd_val": sums["validation"]["maxDD"],
            "dd_test": sums["test"]["maxDD"],
            "avgNet": sums["full_2y"]["avgNet"],
            "full_wr": sums["full_2y"]["wr"],
            "val_wr": sums["validation"]["wr"],
            "test_wr": sums["test"]["wr"],
            "trainval_days": n_trainval_days,
        }
        rows.append(row)
        variant_results[variant_name] = {"short_model": short_model, "short_scored": short_scored, "long_scored_clean": long_scored_for_variant, "combined_sums": sums, "row": row, "no_overlap_audit": no_overlap_audit}
        subsection(f"COMBINED MANDATORY ML RESULT — {variant_name}")
        for sp in ["train", "validation", "test", "full_2y"]:
            m = sums[sp]
            print(f"{sp:<10} trades={m['taken']:>5,} cov={m['coverage']*100:>6.2f}% tpd={m['tpd']:>6.3f} WR={m['wr']*100:>6.2f}% PF={fmt_pf(m['pf']):>6} net={m['net']*100:>8.3f}% DD={m['maxDD']*100:>7.3f}% avgNet={m['avgNet']*10000:>7.2f}bp")

    section("FINAL VERSION B RESULT AFTER MANDATORY ML")
    print_combined_variant_table(rows)
    selected_row = select_final_variant(rows)
    selected_variant = selected_row["variant"]
    section("FINAL VERSION B SELECTED")
    print(f"version_b_short_variant={selected_variant}")
    print(f"version_b_short_rule_filter={short_variants[selected_variant].filter_name}")
    print("selection_target_tpd=DISABLED_NO_FORCED_TPD_TARGET")
    print(f"selection_trainval_tpd_report_only={selected_row['trainval_tpd']:.3f}")
    print("selection_basis=train+validation only; test is audit only")
    print(f"FINAL_FULL_2Y_TRADES={selected_row['full_trades']:,}")
    print(f"FINAL_FULL_2Y_TPD={selected_row['full_tpd']:.3f}")
    print(f"FINAL_FULL_2Y_NET={selected_row['full_net']*100:.3f}%")
    print(f"FINAL_FULL_2Y_PF={fmt_pf(selected_row['pf_full'])}")
    print(f"FINAL_FULL_2Y_MAXDD={selected_row['dd_full']*100:.3f}%")
    print(f"FINAL_FULL_2Y_AVGNET={selected_row['avgNet']*10000:.2f}bp")

    result = {
        "long_model": long_model,
        "long_scored": variant_results[selected_variant].get("long_scored_clean", long_scored),
        "long_scored_raw_ml": long_scored,
        "variant_results": variant_results,
        "comparison_rows": rows,
        "selected_variant": selected_variant,
        "selected_row": selected_row,
        "global_no_overlap_audit": variant_results[selected_variant].get("no_overlap_audit", {}),
    }
    if SAVE_ML_ARTIFACTS:
        save_final_artifacts(result, short_variants)
    else:
        section("ML ARTIFACTS NOT SAVED")
        print(f"SAVE_ML_ARTIFACTS={SAVE_ML_ARTIFACTS}")
        print("Console-only run. No joblib/config/files are written.")
    return result


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items() if k not in {"model", "scored_trades", "long_scored", "short_scored"}}
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
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return str(obj)
    return obj


def save_final_artifacts(result: Dict[str, Any], short_variants: Dict[str, RuleVariant]) -> None:
    section("SAVE FINAL V22 ML ARTIFACTS / CONFIG / FEATURES")

    try:
        import joblib
    except Exception as e:
        die(f"joblib is required for saving model files but could not be imported: {e}")

    ML_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    selected_variant = str(result["selected_variant"])
    long_model_info = result["long_model"]
    short_model_info = result["variant_results"][selected_variant]["short_model"]
    selected_row = result["selected_row"]

    long_features = list(long_model_info.get("feature_cols", []))
    short_features = list(short_model_info.get("feature_cols", []))

    long_model_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_long_ml_model.joblib"
    short_model_path = ML_ARTIFACT_DIR / f"ethusdt_15m_{selected_variant.lower()}_ml_model.joblib"
    bundle_path = ML_ARTIFACT_DIR / ML_BUNDLE_FILE
    config_path = ML_ARTIFACT_DIR / ML_CONFIG_FILE
    long_features_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_long_ml_features.json"
    short_features_path = ML_ARTIFACT_DIR / f"ethusdt_15m_{selected_variant.lower()}_ml_features.json"
    comparison_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_final_comparison_rows.csv"
    selected_row_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_selected_row.json"
    final_trade_log_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_final_ml_taken_trades.csv"
    audit_summary_path = ML_ARTIFACT_DIR / "ethusdt_15m_v22_final_export_audit_summary.json"

    # Save individual calibrated models.
    joblib.dump(long_model_info["model"], long_model_path)
    joblib.dump(short_model_info["model"], short_model_path)

    # Save final selected ML-taken trades for audit/parity.
    long_scored = result.get("long_scored", pd.DataFrame()).copy()
    short_scored = result["variant_results"][selected_variant].get("short_scored", pd.DataFrame()).copy()
    scored_parts = []
    if not long_scored.empty:
        scored_parts.append(long_scored)
    if not short_scored.empty:
        scored_parts.append(short_scored)
    if scored_parts:
        final_scored = pd.concat(scored_parts, ignore_index=True, sort=False)
        if "ml_take" in final_scored.columns:
            final_taken = final_scored[final_scored["ml_take"].astype(bool)].copy()
        else:
            final_taken = final_scored.copy()
        sort_cols = [c for c in ["signal_i", "entry_time", "side"] if c in final_taken.columns]
        if sort_cols:
            final_taken = final_taken.sort_values(sort_cols).reset_index(drop=True)
        final_taken.to_csv(final_trade_log_path, index=False)
    else:
        pd.DataFrame().to_csv(final_trade_log_path, index=False)

    comparison_df = pd.DataFrame(result.get("comparison_rows", []))
    comparison_df.to_csv(comparison_path, index=False)
    selected_row_path.write_text(json.dumps(to_jsonable(selected_row), indent=2, default=str), encoding="utf-8")

    # Save feature lists.
    long_features_path.write_text(json.dumps({
        "side": "LONG",
        "engine": "V22_LONG",
        "variant": V22_SELECTED_VARIANT_NAME,
        "feature_count": len(long_features),
        "features": long_features,
    }, indent=2), encoding="utf-8")

    short_features_path.write_text(json.dumps({
        "side": "SHORT",
        "variant": selected_variant,
        "feature_count": len(short_features),
        "features": short_features,
    }, indent=2), encoding="utf-8")

    # Bundle for live/back-end loading.
    bundle = {
        "symbol": SYMBOL,
        "base_tf": BASE_TF,
        "base_dir": str(BASE_DIR),
        "created_by_script": "ethusdt_v22_global_no_overlap_threshold_selection_SAVE_MODEL_FILES_FINAL.py",
        "rule_source": "accepted_no_save_v22_ml_run_converted_to_save_files",
        "long_engine": "V22_LONG",
        "long_variant": V22_SELECTED_VARIANT_NAME,
        "short_variant": selected_variant,
        "long_model": long_model_info["model"],
        "short_model": short_model_info["model"],
        "long_threshold": float(long_model_info["threshold"]),
        "short_threshold": float(short_model_info["threshold"]),
        "long_feature_cols": long_features,
        "short_feature_cols": short_features,
        "long_calibration_method": str(long_model_info.get("calibration_method", "unknown")),
        "short_calibration_method": str(short_model_info.get("calibration_method", "unknown")),
        "short_rule_setup": SHORT_LOCKED_SETUP_NAME,
        "short_rule_trigger": SHORT_LOCKED_TRIGGER,
        "short_rule_filter": selected_variant,
        "short_exit_name": SHORT_EXIT_NAME,
        "v22_long_source_file": str(V22_ENRICHED_TRADE_LOG_FILE),
        "selection_basis": "train+validation only; test audit only",
    }
    joblib.dump(bundle, bundle_path)

    config = {
        "symbol": SYMBOL,
        "base_tf": BASE_TF,
        "base_dir": str(BASE_DIR),
        "artifact_dir": str(ML_ARTIFACT_DIR),
        "bundle_file": str(bundle_path),
        "long_model_file": str(long_model_path),
        "short_model_file": str(short_model_path),
        "long_features_file": str(long_features_path),
        "short_features_file": str(short_features_path),
        "final_trade_log_file": str(final_trade_log_path),
        "comparison_rows_file": str(comparison_path),
        "selected_row_file": str(selected_row_path),
        "long_engine": "V22_LONG",
        "long_variant": V22_SELECTED_VARIANT_NAME,
        "short_variant": selected_variant,
        "long_threshold": float(long_model_info["threshold"]),
        "short_threshold": float(short_model_info["threshold"]),
        "long_feature_count": len(long_features),
        "short_feature_count": len(short_features),
        "final_selected_row": to_jsonable(selected_row),
        "global_no_overlap_after_ml": bool(GLOBAL_NO_OVERLAP_AFTER_ML),
        "global_no_overlap_same_bar_priority": GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY,
        "flip_policy": "NO_FLIP_IGNORE_OPPOSITE_SIGNAL_WHILE_POSITION_OPEN" if NO_FLIP_ON_OPPOSITE_SIGNAL else "UNDEFINED",
        "no_forced_target_tpd": True,
        "global_no_overlap_audit": to_jsonable(result.get("global_no_overlap_audit", {})),
        "previous_unclean_reference_result": {
            "full_trades": 4234,
            "full_tpd": 5.800,
            "full_net_pct": 564.880,
            "full_pf": 1.491,
            "full_maxdd_pct": 14.091,
            "full_avg_net_bp": 13.34,
            "note": "Previous accepted V22 ML result before global no-overlap clean portfolio filter.",
        },
        "ml_logic_changed": False,
        "short_logic_changed": False,
        "test_usage": "audit only",
    }
    config_path.write_text(json.dumps(to_jsonable(config), indent=2, default=str), encoding="utf-8")

    audit_summary = {
        "overall_status": "SAVED_CLEAN_GLOBAL_NO_OVERLAP",
        "save_ml_artifacts": bool(SAVE_ML_ARTIFACTS),
        "selected_short_variant": selected_variant,
        "long_variant": V22_SELECTED_VARIANT_NAME,
        "long_threshold": float(long_model_info["threshold"]),
        "short_threshold": float(short_model_info["threshold"]),
        "long_feature_count": len(long_features),
        "short_feature_count": len(short_features),
        "selected_full_trades": int(selected_row.get("full_trades", -1)),
        "selected_full_tpd": float(selected_row.get("full_tpd", np.nan)),
        "selected_full_net_pct": float(selected_row.get("full_net", np.nan)) * 100.0,
        "selected_full_pf": float(selected_row.get("pf_full", np.nan)),
        "selected_full_maxdd_pct": float(selected_row.get("dd_full", np.nan)) * 100.0,
        "selected_full_avg_net_bp": float(selected_row.get("avgNet", np.nan)) * 10000.0,
        "global_no_overlap_after_ml": bool(GLOBAL_NO_OVERLAP_AFTER_ML),
        "global_no_overlap_same_bar_priority": GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY,
        "flip_policy": "NO_FLIP_IGNORE_OPPOSITE_SIGNAL_WHILE_POSITION_OPEN" if NO_FLIP_ON_OPPOSITE_SIGNAL else "UNDEFINED",
        "no_forced_target_tpd": True,
        "global_no_overlap_audit": to_jsonable(result.get("global_no_overlap_audit", {})),
        "files": {
            "long_model": str(long_model_path),
            "short_model": str(short_model_path),
            "bundle": str(bundle_path),
            "config": str(config_path),
            "long_features": str(long_features_path),
            "short_features": str(short_features_path),
            "comparison_rows": str(comparison_path),
            "selected_row": str(selected_row_path),
            "final_trade_log": str(final_trade_log_path),
        },
    }
    audit_summary_path.write_text(json.dumps(to_jsonable(audit_summary), indent=2, default=str), encoding="utf-8")

    print(f"Saved long model: {long_model_path}")
    print(f"Saved short model: {short_model_path}")
    print(f"Saved live/backend bundle: {bundle_path}")
    print(f"Saved config: {config_path}")
    print(f"Saved long features: {long_features_path}")
    print(f"Saved short features: {short_features_path}")
    print(f"Saved final taken trades: {final_trade_log_path}")
    print(f"Saved audit summary: {audit_summary_path}")

# ==============================================================================
# CONFIRMATION AUDITS
# ==============================================================================

def audit_line(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "CHECK"
    print(f"{name:<70} {status:<6} {detail}")


def run_safety_audits(panel: pd.DataFrame, splits: Dict[str, SplitDef], shortlist: pd.DataFrame, specs: List[FeatureSpec]) -> None:
    section("CONFIRMATION AUDIT 1 — SAFETY / NO LOOKAHEAD / SPLITS / FEATURE LEAKAGE")
    print("No-lookahead HTF audit:")
    total_future = 0
    for tf in HTF_TFS:
        c = f"{tf}__ts_close"
        future = int((panel[c] > panel["ts_close"]).sum()) if c in panel.columns else -1
        total_future += max(0, future)
        audit_line(f"{tf} merge_asof backward future_rows", future == 0, f"future_rows={future}")
    audit_line("TOTAL_HTF_FUTURE_ROWS", total_future == 0, f"total_future_rows={total_future}")

    print("\nExecution-column audit:")
    valid_rows = int(panel["valid_next_entry"].sum()) if "valid_next_entry" in panel.columns else 0
    bad_gap = int((panel["entry_gap_minutes"].notna() & ~np.isclose(panel["entry_gap_minutes"].fillna(-9999.0), EXPECTED_NEXT_MINUTES)).sum()) if "entry_gap_minutes" in panel.columns else -1
    last_blocked = (not bool(panel["valid_next_entry"].iloc[-1])) if "valid_next_entry" in panel.columns and len(panel) else False
    audit_line("valid_next_entry rows", valid_rows == len(panel) - 1, f"valid={valid_rows:,}/{len(panel):,}")
    audit_line("entry_gap_minutes equals 15m", bad_gap == 0, f"bad_15m_gaps={bad_gap}")
    audit_line("last row blocked as signal", last_blocked, f"last_row_blocked={last_blocked}")

    print("\nSplit audit:")
    tr, va, te = splits["train"], splits["validation"], splits["test"]
    contiguous = tr.end == va.start and va.end == te.start and tr.start == 0 and te.end == len(panel)
    time_order = panel.iloc[tr.end - 1]["ts_open"] < panel.iloc[va.start]["ts_open"] < panel.iloc[te.start]["ts_open"]
    audit_line("split index contiguity", contiguous, f"train={tr.start}:{tr.end} val={va.start}:{va.end} test={te.start}:{te.end}")
    audit_line("split chronological order", bool(time_order), f"train_end={panel.iloc[tr.end-1]['ts_open']} val_start={panel.iloc[va.start]['ts_open']} test_start={panel.iloc[te.start]['ts_open']}")
    print("selection_basis=train+validation only; test=audit only")

    print("\nFeature leakage audit:")
    leaky_panel_cols = [c for c in panel.columns if is_leaky_col(str(c))]
    leaky_spec_cols = [sp.source_column for sp in specs if is_leaky_col(sp.source_column)]
    leaky_shortlist_cols = [c for c in shortlist.columns if is_leaky_col(str(c))]
    audit_line("leaky panel columns used by selected specs", len(leaky_spec_cols) == 0, f"leaky_used_specs={len(leaky_spec_cols)}")
    audit_line("raw panel columns with suspicious names", len(leaky_panel_cols) == 0, f"raw_panel_suspicious_cols={len(leaky_panel_cols)}")
    audit_line("shortlist metadata suspicious names", True, f"metadata_cols_with_patterns={len(leaky_shortlist_cols)}; not used as feature inputs")
    if leaky_spec_cols:
        print("leaky_spec_columns=", leaky_spec_cols[:30])
    if leaky_panel_cols:
        print("raw_panel_suspicious_columns_sample=", leaky_panel_cols[:30])


def run_trade_execution_audit(trades: pd.DataFrame, panel: pd.DataFrame, label: str) -> None:
    section(f"CONFIRMATION AUDIT 2 — TRADE EXECUTION — {label}")
    if trades is None or trades.empty:
        print("No trades to audit.")
        return
    entry_next = pd.to_numeric(trades["entry_i"], errors="coerce") == pd.to_numeric(trades["signal_i"], errors="coerce") + 1
    same_candle = int((pd.to_numeric(trades["entry_i"], errors="coerce") <= pd.to_numeric(trades["signal_i"], errors="coerce")).sum())
    gaps = panel.loc[trades["signal_i"].astype(int), "entry_gap_minutes"].reset_index(drop=True)
    bad_gaps = int((gaps.notna() & ~np.isclose(pd.to_numeric(gaps, errors="coerce").fillna(-9999.0), EXPECTED_NEXT_MINUTES)).sum())
    ordered = trades.sort_values("entry_i").reset_index(drop=True)
    overlap_violations = int((ordered["signal_i"].iloc[1:].to_numpy() <= ordered["exit_i"].iloc[:-1].to_numpy()).sum()) if len(ordered) > 1 else 0
    audit_line("entry_i == signal_i + 1", bool(entry_next.all()), f"bad_entries={int((~entry_next).sum())}")
    audit_line("no same-candle entry", same_candle == 0, f"same_candle_entries={same_candle}")
    audit_line("signal-to-entry gap = 15m", bad_gaps == 0, f"bad_gaps={bad_gaps}")
    audit_line("NO_OVERLAP respected", overlap_violations == 0 if NO_OVERLAP else True, f"overlap_violations={overlap_violations}")

def describe_overlap_trade(row: pd.Series, prefix: str) -> str:
    def g(k: str, default: Any = "") -> Any:
        return row[k] if k in row.index else default
    fields = [
        f"{prefix}_side={g('side')}",
        f"{prefix}_variant={g('variant', g('rule_source', ''))}",
        f"{prefix}_rule_exit={g('rule_exit', '')}",
        f"{prefix}_signal_i={int(g('signal_i', -1)) if pd.notna(g('signal_i', np.nan)) else 'nan'}",
        f"{prefix}_entry_i={int(g('entry_i', -1)) if pd.notna(g('entry_i', np.nan)) else 'nan'}",
        f"{prefix}_exit_i={int(g('exit_i', -1)) if pd.notna(g('exit_i', np.nan)) else 'nan'}",
        f"{prefix}_signal_time={g('signal_time', '')}",
        f"{prefix}_entry_time={g('entry_time', '')}",
        f"{prefix}_exit_time={g('exit_time', '')}",
    ]
    optional_fields = ["reason", "net", "gross", "ml_prob", "ml_threshold", "ml_take", "split"]
    for k in optional_fields:
        if k in row.index:
            v = g(k)
            if isinstance(v, (float, np.floating)) and k in {"net", "gross", "ml_prob", "ml_threshold"}:
                fields.append(f"{prefix}_{k}={float(v):.6f}")
            else:
                fields.append(f"{prefix}_{k}={v}")
    return " | ".join(fields)


def diagnose_overlap_trades(trades: pd.DataFrame, panel: pd.DataFrame, label: str) -> None:
    section(f"OVERLAP DIAGNOSTIC — {label}")
    print("Diagnostic only. No entry, exit, ML, selection, or results logic is changed.")
    print("Primary clean check is entry overlap: next_entry_i <= previous_exit_i.")
    if trades is None or trades.empty:
        print("No trades to diagnose.")
        return
    need = {"signal_i", "entry_i", "exit_i", "side"}
    missing = sorted([c for c in need if c not in trades.columns])
    if missing:
        print(f"Cannot diagnose overlaps; missing_columns={missing}")
        return
    ordered = trades.sort_values(["entry_i", "signal_i", "side"]).reset_index(drop=True).copy()
    signal_rows = []
    entry_rows = []
    for i in range(1, len(ordered)):
        prev = ordered.iloc[i - 1]
        cur = ordered.iloc[i]
        prev_exit_i = int(prev["exit_i"])
        cur_signal_i = int(cur["signal_i"])
        cur_entry_i = int(cur["entry_i"])
        by_signal_rule = cur_signal_i <= prev_exit_i
        by_entry_rule = cur_entry_i <= prev_exit_i
        if by_signal_rule:
            signal_rows.append((i, prev, cur, by_entry_rule))
        if by_entry_rule:
            entry_rows.append((i, prev, cur, by_entry_rule))

    same_side = 0
    cross_side = 0
    for _, prev, cur, _ in entry_rows:
        ps = str(prev.get("side", ""))
        cs = str(cur.get("side", ""))
        same_side += int(ps == cs)
        cross_side += int(ps != cs)

    print(f"signal_overlap_count_by_existing_audit_rule={len(signal_rows)}")
    print(f"entry_overlap_count={len(entry_rows)}")
    print(f"entry_overlap_status={'PASS' if len(entry_rows) == 0 else 'FAIL'}")

    if not entry_rows:
        if signal_rows:
            print("note=Signal-time overlap exists, but actual entry overlap is zero; next entry starts after previous exit.")
        else:
            print("No signal or entry overlaps found.")
        return

    for k, (i, prev, cur, by_entry_rule) in enumerate(entry_rows, 1):
        ps = str(prev.get("side", ""))
        cs = str(cur.get("side", ""))
        prev_exit_i = int(prev["exit_i"])
        cur_signal_i = int(cur["signal_i"])
        cur_entry_i = int(cur["entry_i"])
        gap_signal_to_prev_exit = cur_signal_i - prev_exit_i
        gap_entry_to_prev_exit = cur_entry_i - prev_exit_i
        print()
        print(f"ENTRY OVERLAP #{k}")
        print(f"type={ps}_then_{cs} same_side={ps == cs} cross_side={ps != cs}")
        print(f"signal_check: next_signal_i <= previous_exit_i -> {cur_signal_i} <= {prev_exit_i} = {cur_signal_i <= prev_exit_i}")
        print(f"entry_overlap_check: next_entry_i <= previous_exit_i -> {cur_entry_i} <= {prev_exit_i} = {by_entry_rule}")
        print(f"gap_signal_to_prev_exit={gap_signal_to_prev_exit} bars | gap_entry_to_prev_exit={gap_entry_to_prev_exit} bars")
        print(describe_overlap_trade(prev, "prev"))
        print(describe_overlap_trade(cur, "next"))

    print()
    print("Entry-overlap summary:")
    print(f"same_side_entry_overlaps={same_side}")
    print(f"cross_side_entry_overlaps={cross_side}")
    print(f"entry_overlap_count={len(entry_rows)}")
    print("probable_reason=Actual entry overlap exists; global chronological no-overlap filter must block these trades.")

def build_final_ml_taken_trades_for_overlap(ml_result: Dict[str, Any]) -> pd.DataFrame:
    selected = ml_result.get("selected_variant")
    frames = []
    long_scored = ml_result.get("long_scored", pd.DataFrame())
    if isinstance(long_scored, pd.DataFrame) and not long_scored.empty:
        frames.append(long_scored[long_scored["ml_take"].astype(bool)].copy())
    short_info = ml_result.get("variant_results", {}).get(selected, {})
    short_scored = short_info.get("short_scored", pd.DataFrame())
    if isinstance(short_scored, pd.DataFrame) and not short_scored.empty:
        frames.append(short_scored[short_scored["ml_take"].astype(bool)].copy())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).sort_values(["entry_i", "signal_i", "side"]).reset_index(drop=True)
    out["diagnostic_stage"] = "FINAL_AFTER_ML_TAKEN"
    return out


def run_raw_split_stability_audit(panel: pd.DataFrame, long_variant: RuleVariant, short_variant: RuleVariant) -> None:
    section("CONFIRMATION AUDIT 3 — RAW RULES TRAIN / VALIDATION / TEST STABILITY")
    combined = pd.concat([long_variant.trades, short_variant.trades], ignore_index=True).sort_values(["signal_i", "side"]).reset_index(drop=True)
    print(f"{'split':<12} {'trades':>7} {'tpd':>7} {'WR':>8} {'PF':>8} {'net':>10} {'DD':>10} {'avgNet':>10}")
    for name, sp in {"train": SplitDef("train", 0, int(len(panel)*TRAIN_RATIO)), "validation": SplitDef("validation", int(len(panel)*TRAIN_RATIO), int(len(panel)*TRAIN_RATIO)+int(len(panel)*VAL_RATIO)), "test": SplitDef("test", int(len(panel)*TRAIN_RATIO)+int(len(panel)*VAL_RATIO), len(panel))}.items():
        part = split_trades(combined, sp)
        s = summarize(part, name, 0, period_days=period_days_from_panel(panel, sp.start, sp.end))
        print(f"{name:<12} {s['trades']:>7,} {s['tpd']:>7.3f} {s['wr']*100:>7.2f}% {fmt_pf(s['pf']):>8} {s['net']*100:>9.3f}% {s['maxDD']*100:>9.3f}% {s['avgNet']*10000:>9.2f}bp")


def run_ml_split_stability_audit(ml_result: Dict[str, Any]) -> None:
    section("CONFIRMATION AUDIT 4 — FINAL ML TRAIN / VALIDATION / TEST STABILITY")
    selected = ml_result["selected_variant"]
    sums = ml_result["variant_results"][selected]["combined_sums"]
    print(f"{'split':<12} {'trades':>7} {'tpd':>7} {'WR':>8} {'PF':>8} {'net':>10} {'DD':>10} {'avgNet':>10}")
    for sp in ["train", "validation", "test", "full_2y"]:
        m = sums[sp]
        print(f"{sp:<12} {m['taken']:>7,} {m['tpd']:>7.3f} {m['wr']*100:>7.2f}% {fmt_pf(m['pf']):>8} {m['net']*100:>9.3f}% {m['maxDD']*100:>9.3f}% {m['avgNet']*10000:>9.2f}bp")
    tr, va, te = sums["train"], sums["validation"], sums["test"]
    audit_line("validation PF positive", va["pf"] > 1.0, f"val_PF={fmt_pf(va['pf'])}")
    audit_line("test PF positive audit", te["pf"] > 1.0, f"test_PF={fmt_pf(te['pf'])}")
    audit_line("validation net positive", va["net"] > 0, f"val_net={va['net']*100:.3f}%")
    audit_line("test net positive audit", te["net"] > 0, f"test_net={te['net']*100:.3f}%")
    print(f"pf_val/train={safe_div(va['pf'], tr['pf'], np.nan):.3f} pf_test/train={safe_div(te['pf'], tr['pf'], np.nan):.3f}")
    print("Note: this cannot prove zero overfitting; it checks whether validation/test collapse versus train.")


def print_fast_mode_confirmation_comparison(selected_row: Dict[str, Any]) -> None:
    section("CONFIRMATION COMPARISON — AGAINST LAST ACCEPTED FAST MODE")
    b = FAST_MODE_BENCHMARK
    c = selected_row
    print(f"{'metric':<14} {'fast_mode':>14} {'confirmation':>14} {'delta':>14}")
    print(f"{'trades':<14} {b['trades']:>14,} {c['full_trades']:>14,} {c['full_trades']-b['trades']:>14,}")
    print(f"{'tpd':<14} {b['tpd']:>14.3f} {c['full_tpd']:>14.3f} {c['full_tpd']-b['tpd']:>14.3f}")
    print(f"{'WR':<14} {b['wr']*100:>13.2f}% {c.get('full_wr', np.nan)*100:>13.2f}% {(c.get('full_wr', np.nan)-b['wr'])*100:>13.2f}%")
    print(f"{'net':<14} {b['net']*100:>13.3f}% {c['full_net']*100:>13.3f}% {(c['full_net']-b['net'])*100:>13.3f}%")
    print(f"{'PF':<14} {b['pf']:>14.3f} {c['pf_full']:>14.3f} {c['pf_full']-b['pf']:>14.3f}")
    print(f"{'DD':<14} {b['dd']*100:>13.3f}% {c['dd_full']*100:>13.3f}% {(c['dd_full']-b['dd'])*100:>13.3f}%")
    print(f"{'avgNet':<14} {b['avgNet']*10000:>13.2f}bp {c['avgNet']*10000:>13.2f}bp {(c['avgNet']-b['avgNet'])*10000:>13.2f}bp")
    stable = (c['pf_full'] >= 1.0 and c.get('full_wr', 0) >= 0.50)
    audit_line("confirmation remains valid without forced TPD target", stable, "forced_target_tpd=disabled; PF>1 and WR>50% check only")

# ==============================================================================
# RAW VARIANT COMPARISON
# ==============================================================================

def print_raw_combined_comparison(panel: pd.DataFrame, long_variant: RuleVariant, short_variants: Dict[str, RuleVariant]) -> None:
    section("RAW VERSION B COMPARISON BEFORE ML")
    print("This is the Version B rule-level result before mandatory ML.")
    print(f"{'variant':<20} {'full_trades':>12} {'full_tpd':>9} {'full_net':>10} {'PF':>8} {'maxDD':>9} {'avgNet':>10}")
    for name in SHORT_EXPANSION_VARIANTS:
        combined = pd.concat([long_variant.trades, short_variants[name].trades], ignore_index=True).sort_values(["signal_i", "side"]).reset_index(drop=True)
        full = summarize(combined, "full_2y", 0, period_days=period_days_from_panel(panel, 0, len(panel)))
        print(f"{name:<20} {full['trades']:>12,} {full['tpd']:>9.3f} {full['net']*100:>9.3f}% {fmt_pf(full['pf']):>8} {full['maxDD']*100:>8.3f}% {full['avgNet']*10000:>9.2f}bp")

# ==============================================================================
# MAIN
# ==============================================================================

def main() -> None:
    global SAVE_ML_ARTIFACTS
    hr("=")
    print("ETHUSDT — V22 LONG + SHORT_NO_FILTER — GLOBAL NO-OVERLAP THRESHOLD SELECTION — SAVE MODEL FILES FINAL")
    hr("=")
    print("Base = original uploaded baseline training code.")
    print("Final export run: SAVE_ML_ARTIFACTS=True. Threshold pair is selected inside global no-overlap simulator.")
    print("ML logic is not changed. SHORT_NO_FILTER is not changed.")
    print("V22 logic = user-provided V2.2 RX4_MIXED original logic; selected variant V22_RX4_MIXED_BALANCED_CUT.")
    print("Comparison: OLD_LONG + SHORT_NO_FILTER + ML vs V22_LONG + SHORT_NO_FILTER + ML.")
    print("Selection uses train + validation only. Test is audit only.")
    print("Final V22 model/joblib/config/feature files will be saved after the V22 ML run only.")
    print("Forced target TPD is disabled. TPD is report-only.")
    print("After ML acceptance, final portfolio uses global one-position no-overlap across LONG and SHORT.")
    print("Flip policy: no flip; opposite-side signals are ignored while a trade is open.")
    print(f"CONFIRMATION_ML: N_TRIALS={ML['N_TRIALS']} N_WF_FOLDS={ML['N_WF_FOLDS']} MAX_SNAPSHOT_FEATURES={ML['MAX_SNAPSHOT_FEATURES']} THR_STEPS={ML['THR_STEPS']}")
    print(f"BASE_DIR={BASE_DIR}")
    print(f"SAVE_ML_ARTIFACTS={SAVE_ML_ARTIFACTS}")
    print(f"Date window: {START_DATE} -> {END_DATE}")
    print(f"ROUND_TRIP_COST={ROUND_TRIP_COST:.6f}")

    panel = load_shared_panel()
    splits = make_splits(panel)
    shortlist = load_shortlist_raw()

    long_specs = build_specs(panel, shortlist, "LONG ENGINE", feature_first=False)
    short_specs = build_specs(panel, shortlist, "SHORT ENGINE", feature_first=SHORT_USE_SHORTLIST_FEATURE_FIELD_FIRST)
    run_safety_audits(panel, splits, shortlist, long_specs + short_specs)

    old_long_variant = run_long_locked(panel, splits, long_specs)
    short_variants = run_short_expansion(panel, splits, short_specs)
    selected_short = short_variants["SHORT_NO_FILTER"]

    v22_long_variant = build_v22_long_variant(panel, splits)

    old_raw_sums = summarize_combined_variant_by_split(panel, old_long_variant, selected_short)
    v22_raw_sums = summarize_combined_variant_by_split(panel, v22_long_variant, selected_short)
    print_old_vs_v22_comparison("RAW BEFORE ML — OLD_LONG+SHORT vs V22_LONG+SHORT", old_raw_sums, v22_raw_sums)

    section("RAW AUDITS — OLD LONG + SHORT")
    old_raw_combined = prepare_raw_trades_for_ml(old_long_variant, selected_short)
    run_trade_execution_audit(old_raw_combined, panel, "OLD RAW LONG+SHORT BEFORE ML")
    diagnose_overlap_trades(old_raw_combined, panel, "OLD RAW LONG+SHORT BEFORE ML")
    run_raw_split_stability_audit(panel, old_long_variant, selected_short)

    section("RAW AUDITS — V22 LONG + SHORT")
    v22_raw_combined = prepare_raw_trades_for_ml(v22_long_variant, selected_short)
    run_trade_execution_audit(v22_raw_combined, panel, "V22 RAW LONG+SHORT BEFORE ML")
    diagnose_overlap_trades(v22_raw_combined, panel, "V22 RAW LONG+SHORT BEFORE ML")
    run_raw_split_stability_audit(panel, v22_long_variant, selected_short)

    section("MANDATORY ML RUN — OLD LONG + SHORT — ORIGINAL BASELINE ML LOGIC — NO ARTIFACT SAVE")
    final_save_flag = SAVE_ML_ARTIFACTS
    SAVE_ML_ARTIFACTS = False
    old_ml_result = run_mandatory_ml_comparison(panel, splits, old_long_variant, short_variants)
    run_ml_split_stability_audit(old_ml_result)
    old_final_ml_taken = build_final_ml_taken_trades_for_overlap(old_ml_result)
    diagnose_overlap_trades(old_final_ml_taken, panel, "OLD FINAL AFTER ML")

    section("MANDATORY ML RUN — V22 LONG + SHORT — SAME BASELINE ML LOGIC — SAVE ARTIFACTS")
    SAVE_ML_ARTIFACTS = final_save_flag
    v22_ml_result = run_mandatory_ml_comparison(panel, splits, v22_long_variant, short_variants)
    run_ml_split_stability_audit(v22_ml_result)
    v22_final_ml_taken = build_final_ml_taken_trades_for_overlap(v22_ml_result)
    diagnose_overlap_trades(v22_final_ml_taken, panel, "V22 FINAL AFTER ML")

    old_ml_sums = ml_selected_sums(old_ml_result)
    v22_ml_sums = ml_selected_sums(v22_ml_result)
    print_old_vs_v22_comparison("FINAL AFTER ML — OLD_LONG+SHORT+ML vs V22_LONG+SHORT+ML", old_ml_sums, v22_ml_sums)

    section("FINAL DECISION DATA — TRAIN+VALIDATION ONLY, TEST AUDIT ONLY")
    old_sel = old_ml_result["selected_row"]
    v22_sel = v22_ml_result["selected_row"]
    old_trainval_net = old_ml_sums["train"]["net"] + old_ml_sums["validation"]["net"]
    v22_trainval_net = v22_ml_sums["train"]["net"] + v22_ml_sums["validation"]["net"]
    print(f"OLD_SELECTED_SHORT={old_ml_result['selected_variant']}")
    print(f"V22_SELECTED_SHORT={v22_ml_result['selected_variant']}")
    print(f"OLD_TRAINVAL_NET={old_trainval_net*100:.3f}%")
    print(f"V22_TRAINVAL_NET={v22_trainval_net*100:.3f}%")
    print(f"DELTA_TRAINVAL_NET={(v22_trainval_net-old_trainval_net)*100:.3f}%")
    print(f"OLD_FULL_NET={old_sel['full_net']*100:.3f}% PF={fmt_pf(old_sel['pf_full'])} DD={old_sel['dd_full']*100:.3f}% TRADES={old_sel['full_trades']:,}")
    print(f"V22_FULL_NET={v22_sel['full_net']*100:.3f}% PF={fmt_pf(v22_sel['pf_full'])} DD={v22_sel['dd_full']*100:.3f}% TRADES={v22_sel['full_trades']:,}")
    print(f"DELTA_FULL_NET={(v22_sel['full_net']-old_sel['full_net'])*100:.3f}%")
    print("TEST_AUDIT_ONLY=True")
    print("MODEL_FILES_SAVED=True")

    section("FINAL LOCKED SETTINGS")
    print("RULE_SOURCE=COMPARE_OLD_LONG_VS_V22_LONG_WITH_SAME_BASELINE_ML")
    print("ML_MODE=MANDATORY_CONSERVATIVE_LIGHT_FILTER_UNCHANGED")
    print("ML_MODEL_STRUCTURE=SEPARATE_LONG_SHORT_MODELS_UNCHANGED")
    print(f"SAVE_ML_ARTIFACTS={SAVE_ML_ARTIFACTS}")
    print("FORCED_TARGET_TPD=DISABLED")
    print(f"GLOBAL_NO_OVERLAP_AFTER_ML={GLOBAL_NO_OVERLAP_AFTER_ML}")
    print(f"GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY={GLOBAL_NO_OVERLAP_SAME_BAR_PRIORITY}")
    print("FLIP_POLICY=NO_FLIP_IGNORE_OPPOSITE_SIGNAL_WHILE_POSITION_OPEN")
    print(f"OLD_LONG_SETUP={LONG_SETUP_NAME}")
    print(f"OLD_LONG_TRIGGER={LONG_TRIGGER}")
    print(f"OLD_LONG_FILTER={old_long_variant.filter_name}")
    print(f"OLD_LONG_EXIT={LONG_EXIT_NAME}")
    print(f"V22_LONG_VARIANT={V22_SELECTED_VARIANT_NAME}")
    print(f"SHORT_SETUP={SHORT_LOCKED_SETUP_NAME}")
    print(f"SHORT_TRIGGER={SHORT_LOCKED_TRIGGER}")
    print(f"SHORT_FILTER=SHORT_NO_FILTER")
    print(f"SHORT_EXIT={SHORT_EXIT_NAME}")
    print(f"OLD_ML_LONG_THRESHOLD={old_ml_result['long_model']['threshold']:.3f}")
    print(f"OLD_ML_SHORT_THRESHOLD={old_ml_result['variant_results'][old_ml_result['selected_variant']]['short_model']['threshold']:.3f}")
    print(f"V22_ML_LONG_THRESHOLD={v22_ml_result['long_model']['threshold']:.3f}")
    print(f"V22_ML_SHORT_THRESHOLD={v22_ml_result['variant_results'][v22_ml_result['selected_variant']]['short_model']['threshold']:.3f}")

    section("DONE")
    print(f"Final export comparison finished. V22 model files saved inside: {ML_ARTIFACT_DIR}")


if __name__ == "__main__":
    main()
