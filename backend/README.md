# Backend — ETHUSDT Trading Bot

This folder contains the Python trading bot.

## Files

- **`live_code.py`** — the runtime bot. Paper/live 15m monitor for ETHUSDT
  (V22 LONG + SHORT_NO_FILTER + mandatory ML). This is what you run in production.
- **`rules_ml_version.py`** — the training script, kept for reference/training only.
  It is **not** part of the live runtime.

## Model artifacts (required)

`live_code.py` cannot run without the trained model artifacts. By default it expects
them under a `model files/` directory:

- `model files/ethusdt_15m_short_expansion_mandatory_ml_live_bundle.joblib`
- `model files/ethusdt_15m_short_expansion_mandatory_ml_config.json`

Place these artifacts (produced by `rules_ml_version.py`) where `live_code.py` expects
them before starting the bot. They are intentionally git-ignored.

## Setup

Create a virtual environment and install the runtime dependencies:

```bash
# from the backend/ directory
python3 -m venv venv
source venv/bin/activate        # Linux / macOS (Hostinger VPS)
# .\venv\Scripts\Activate.ps1   # Windows PowerShell

pip install --upgrade pip
pip install -r requirements.txt
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

See `.env.example` for the supported variables (Binance endpoints and email alert
credentials). The bot uses built-in defaults if a variable is unset.

## Run

```bash
source venv/bin/activate
python live_code.py
```
