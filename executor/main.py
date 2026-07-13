"""Executor service entrypoint.

Binance USD-M Futures execution service — scaffold only.
This build supports EXECUTION_MODE=OFF exclusively and makes zero network calls.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

ALLOWED_MODES = ("OFF", "TESTNET_READ", "TESTNET_TRADE", "LIVE_READ", "LIVE_DRYRUN", "LIVE")

HEARTBEAT_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("executor")


def main() -> int:
    mode = os.environ.get("EXECUTION_MODE", "").strip() or "OFF"

    if mode not in ALLOWED_MODES:
        log.error(
            "Invalid EXECUTION_MODE=%r — allowed values: %s", mode, ", ".join(ALLOWED_MODES)
        )
        return 1

    if mode != "OFF":
        log.error(
            "EXECUTION_MODE=%s requested but this build supports OFF only — refusing to start",
            mode,
        )
        return 1

    log.info("=" * 60)
    log.info("executor starting | mode=%s", mode)
    log.info("scaffold build: no Binance connectivity, no trading logic")
    log.info("=" * 60)

    while True:
        now = datetime.now(timezone.utc).isoformat()
        log.info("executor alive | mode=OFF | %s", now)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
