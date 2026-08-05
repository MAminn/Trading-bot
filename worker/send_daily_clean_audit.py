#!/usr/bin/env python3
import argparse
import csv
import io
import json
import os
import smtplib
import tempfile
import zipfile
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Optional, List

ENV_PATH = Path("/opt/trading-bot/worker/.env")
CLEAN_DIR = Path("/var/lib/docker/volumes/worker_audit/_data/shadow_live_v22_clean_audit")

MASTER_NAME = "shadow_live_master_audit.jsonl"
TRADES_NAME = "shadow_live_trades.csv"
STATE_NAME = "shadow_live_state.json"
ERRORS_NAME = "shadow_live_errors.jsonl"

EXACT_FILES = [MASTER_NAME, TRADES_NAME, STATE_NAME, ERRORS_NAME]


def parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def record_time_master(rec: Dict[str, Any]) -> Optional[datetime]:
    decision = rec.get("decision") or {}
    bar = rec.get("bar") or {}
    return (
        parse_dt(decision.get("t"))
        or parse_dt(bar.get("ts_open"))
        or parse_dt(rec.get("time_utc"))
    )


def filter_master(src: Path, dst: Path, cutoff: datetime) -> int:
    count = 0
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return 0
    with src.open("r", encoding="utf-8", errors="ignore") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = record_time_master(rec)
            if ts and ts >= cutoff:
                fout.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                count += 1
    return count


def filter_errors(src: Path, dst: Path, cutoff: datetime) -> int:
    count = 0
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return 0
    with src.open("r", encoding="utf-8", errors="ignore") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = parse_dt(rec.get("time_utc"))
            if ts and ts >= cutoff:
                fout.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                count += 1
    return count


def filter_trades(src: Path, dst: Path, cutoff: datetime) -> int:
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return 0

    with src.open("r", encoding="utf-8", errors="ignore", newline="") as fin:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames or []
        rows: List[Dict[str, Any]] = []
        for row in reader:
            ts = parse_dt(row.get("logged_at_utc")) or parse_dt(row.get("exit_t"))
            if ts and ts >= cutoff:
                rows.append(row)

    with dst.open("w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return len(rows)


def copy_state(src: Path, dst: Path) -> None:
    if src.exists():
        dst.write_text(src.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    else:
        dst.write_text(json.dumps({"missing_state": True}, indent=2), encoding="utf-8")


def build_24h_export(hours: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    tmpdir = Path(tempfile.mkdtemp(prefix="helix_24h_audit_"))

    master_count = filter_master(CLEAN_DIR / MASTER_NAME, tmpdir / MASTER_NAME, cutoff)
    trades_count = filter_trades(CLEAN_DIR / TRADES_NAME, tmpdir / TRADES_NAME, cutoff)
    copy_state(CLEAN_DIR / STATE_NAME, tmpdir / STATE_NAME)
    errors_count = filter_errors(CLEAN_DIR / ERRORS_NAME, tmpdir / ERRORS_NAME, cutoff)

    zip_path = tmpdir / "ethusdt_real_live_4file_audit_last_24h.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in EXACT_FILES:
            zf.write(tmpdir / name, arcname=name)

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(zf.namelist())
    if names != sorted(EXACT_FILES):
        raise RuntimeError(f"ZIP exact-file check failed: {names}")

    return {
        "now_utc": now.isoformat(),
        "cutoff_utc": cutoff.isoformat(),
        "hours": hours,
        "tmpdir": str(tmpdir),
        "zip_path": str(zip_path),
        "zip_size_bytes": zip_path.stat().st_size,
        "master_records": master_count,
        "closed_trades": trades_count,
        "error_records": errors_count,
        "zip_names": names,
    }


def send_email(env: Dict[str, str], export: Dict[str, Any]) -> None:
    sender = env.get("LIVE_EMAIL_ADDRESS", "")
    password = env.get("LIVE_EMAIL_APP_PASSWORD", "")
    recipient = env.get("ADMIN_AUDIT_EMAIL", "Omarameen291@gmail.com") or "Omarameen291@gmail.com"

    if not sender or not password or not recipient:
        raise RuntimeError("Missing LIVE_EMAIL_ADDRESS / LIVE_EMAIL_APP_PASSWORD / ADMIN_AUDIT_EMAIL")

    msg = EmailMessage()
    msg["Subject"] = f"Helix ETHUSDT daily 4-file audit | last {export['hours']}h"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(
        "Attached ZIP contains exactly the 4 admin audit files filtered to the past period.\n\n"
        f"Window start UTC: {export['cutoff_utc']}\n"
        f"Window end UTC: {export['now_utc']}\n"
        f"Master audit records: {export['master_records']}\n"
        f"Closed trades: {export['closed_trades']}\n"
        f"Error records: {export['error_records']}\n"
        f"Files: {', '.join(EXACT_FILES)}\n"
    )

    zip_bytes = Path(export["zip_path"]).read_bytes()
    msg.add_attachment(
        zip_bytes,
        maintype="application",
        subtype="zip",
        filename="ethusdt_real_live_4file_audit_last_24h.zip",
    )

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender, password.replace(" ", ""))
        smtp.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    env = parse_env(ENV_PATH)
    export = build_24h_export(args.hours)

    print("DAILY_EXPORT_BUILD=PASS")
    print("ZIP_PATH=" + export["zip_path"])
    print("ZIP_SIZE_BYTES=" + str(export["zip_size_bytes"]))
    print("MASTER_RECORDS=" + str(export["master_records"]))
    print("CLOSED_TRADES=" + str(export["closed_trades"]))
    print("ERROR_RECORDS=" + str(export["error_records"]))
    print("ZIP_NAMES=" + ",".join(export["zip_names"]))
    print("EMAIL_SENDER_SET=" + ("YES" if env.get("LIVE_EMAIL_ADDRESS") else "NO"))
    print("EMAIL_PASSWORD_SET=" + ("YES" if env.get("LIVE_EMAIL_APP_PASSWORD") else "NO"))
    print("EMAIL_RECIPIENT=" + (env.get("ADMIN_AUDIT_EMAIL") or "Omarameen291@gmail.com"))

    if args.send:
        send_email(env, export)
        print("EMAIL_SEND=PASS")
    else:
        print("EMAIL_SEND=SKIPPED_DRY_RUN")


if __name__ == "__main__":
    main()
