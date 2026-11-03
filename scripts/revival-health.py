#!/usr/bin/env python3
"""Revival Radar scan-health monitor, independent of any website.

Replaces the retired `onchainflow-health` job. It answers one question: *is the scanner
doing its work?* It never touches the Revival database for writing and never changes scan
logic. Zero candidates / zero alerts are healthy: a quiet market is not an outage.

Checks (each is a named condition):
  scan-stale       newest finished scan_runs row is older than --max-age (default 1200 s,
                   i.e. 4 scan intervals of 300 s)
  sources-down     the last --sources-window finished scans all had sources_ok = 0
  db-unreadable    the SQLite file cannot be opened/queried read-only (2 runs in a row)
  disk-low         free space on the data filesystem below --min-free-pct / --min-free-gb
  container-down   the Revival bot container is not running (skipped without docker)

Alerting: Telegram via the Revival bot credentials (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
from its .env, falling back to the Elysium TG_BOT_TOKEN / TG_CHAT_ID). One message per
condition per --cooldown (default 6 h) and one recovery message when it clears. State is
kept in a small JSON file. Credentials are read from the env files only; they are never
printed or logged. Exit status: 0 healthy, 1 unhealthy, 2 usage/configuration error.

    revival-health.py --db /opt/gmgn-revival-radar/data/revival_radar.db
    revival-health.py --db ... --no-alert --json       # inspect without messaging
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("revival-health")

DEFAULT_DB = "/opt/gmgn-revival-radar/data/revival_radar.db"
DEFAULT_STATE = "/var/lib/elysium-ops/revival-health.json"
DEFAULT_CONTAINER = "gmgn-revival-radar-radar-1"

# conditions that need several consecutive failing runs before they alert (transient locks)
CONFIRM_RUNS = {"db-unreadable": 2}


@dataclass(frozen=True)
class Finding:
    condition: str
    message: str


# ------------------------------------------------------------------ evaluation (pure-ish)
def parse_ts(value: object) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string (naive = UTC); else None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v          # tolerate epoch milliseconds
    s = str(value).strip()
    if not s:
        return None
    try:
        return parse_ts(float(s))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def open_ro(path: str, busy_ms: int = 5000) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True,
                           timeout=busy_ms / 1000.0)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def check_scans(conn: sqlite3.Connection, now: float, max_age: float,
                sources_window: int) -> list[Finding]:
    out: list[Finding] = []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_runs)")}
    if "finished" not in cols:
        return [Finding("db-unreadable", "scan_runs has no 'finished' column (schema changed?)")]
    want = [c for c in ("id", "started", "finished", "sources_ok") if c in cols]
    rows = conn.execute(
        f"SELECT {', '.join(want)} FROM scan_runs ORDER BY id DESC LIMIT 50").fetchall()
    recs = [dict(zip(want, r, strict=True)) for r in rows]
    finished = [r for r in recs if r.get("finished") not in (None, "")]
    last = parse_ts(finished[0]["finished"]) if finished else None
    if last is None:
        out.append(Finding("scan-stale", "no finished scan found in scan_runs"))
    elif now - last > max_age:
        running = bool(recs and recs[0].get("finished") in (None, ""))
        out.append(Finding(
            "scan-stale",
            f"last finished scan was {int((now - last) // 60)} min ago "
            f"(limit {int(max_age // 60)} min){'; a newer run is unfinished' if running else ''}"))
    if "sources_ok" in cols and len(finished) >= sources_window > 0:
        recent = finished[:sources_window]
        if all((r.get("sources_ok") or 0) == 0 for r in recent):
            out.append(Finding("sources-down",
                               f"the last {sources_window} scans all had zero working sources"))
    return out


def check_disk(path: str, min_free_pct: float, min_free_gb: float) -> list[Finding]:
    target = path if os.path.exists(path) else os.path.dirname(path) or "/"
    du = shutil.disk_usage(target)
    free_pct = 100.0 * du.free / du.total if du.total else 0.0
    free_gb = du.free / 1e9
    if free_pct < min_free_pct or free_gb < min_free_gb:
        return [Finding("disk-low", f"{free_gb:.1f} GB free ({free_pct:.0f}%) on the data disk")]
    return []


def check_container(name: str) -> list[Finding]:
    if not name or shutil.which("docker") is None:
        return []
    try:
        p = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", name],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    status = p.stdout.strip()
    if p.returncode != 0:
        return [Finding("container-down", f"container {name} not found")]
    if status != "running":
        return [Finding("container-down", f"container {name} is {status}")]
    return []


def evaluate(args: argparse.Namespace, now: float) -> list[Finding]:
    findings: list[Finding] = []
    try:
        conn = open_ro(args.db)
        try:
            findings += check_scans(conn, now, args.max_age, args.sources_window)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        findings.append(Finding("db-unreadable", f"{type(e).__name__}: {e}"))
    findings += check_disk(args.db, args.min_free_pct, args.min_free_gb)
    findings += check_container(args.container)
    return findings


# ------------------------------------------------------------------ alert decisions
def decide(state: dict, findings: list[Finding], now: float, cooldown: float
           ) -> tuple[list[str], dict]:
    """(messages to send, new state). Messages are only 'sent' once the caller confirms:
    `last_sent` is updated here optimistically and rolled back by the caller on failure."""
    conds: dict = {k: dict(v) for k, v in (state.get("conditions") or {}).items()}
    active = {f.condition: f for f in findings}
    msgs: list[str] = []
    for name, f in active.items():
        c = conds.setdefault(name, {"fails": 0, "last_sent": 0.0, "alerting": False})
        c["fails"] = int(c.get("fails", 0)) + 1
        if c["fails"] < CONFIRM_RUNS.get(name, 1):
            continue
        if not c.get("alerting") or now - float(c.get("last_sent", 0)) >= cooldown:
            msgs.append(f"⚠️ Revival Radar [{name}]: {f.message}")
            c["last_sent"] = now
            c["alerting"] = True
    for name in list(conds):
        if name not in active:
            if conds[name].get("alerting"):
                msgs.append(f"✅ Revival Radar [{name}]: recovered")
            del conds[name]
    return msgs, {"conditions": conds, "checked_at": now}


def load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".rh-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ telegram
def read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def telegram_credentials(env_files: list[str]) -> tuple[str, str] | None:
    for f in env_files:
        e = read_env_file(f)
        token = e.get("TELEGRAM_BOT_TOKEN") or e.get("TG_BOT_TOKEN")
        chat = e.get("TELEGRAM_CHAT_ID") or e.get("TG_CHAT_ID")
        if token and chat:
            return token, chat
    return None


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0) -> bool:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https host)
            return r.status == 200
    except Exception as e:                       # never log the URL: it contains the token
        log.warning("telegram send failed: %s", type(e).__name__)
        return False


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--state", default=DEFAULT_STATE)
    p.add_argument("--max-age", type=float, default=1200.0, help="seconds (default 1200)")
    p.add_argument("--sources-window", type=int, default=3)
    p.add_argument("--min-free-pct", type=float, default=15.0)
    p.add_argument("--min-free-gb", type=float, default=8.0)
    p.add_argument("--container", default=DEFAULT_CONTAINER, help="'' to skip the container check")
    p.add_argument("--cooldown", type=float, default=6 * 3600.0, help="seconds between repeats")
    p.add_argument("--env-file", action="append", dest="env_files",
                   help="env file(s) holding the Telegram credentials (first complete one wins)")
    p.add_argument("--no-alert", action="store_true", help="evaluate and print only")
    p.add_argument("--json", action="store_true", help="print findings as JSON")
    p.add_argument("--test-telegram", action="store_true",
                   help="send one confirmation message and exit (checks credentials/delivery)")
    return p


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    if args.env_files is None:
        args.env_files = ["/opt/gmgn-revival-radar/.env", "/root/elysium-wallet-tracker/.env"]
    now = time.time() if now is None else now

    if args.test_telegram:
        creds = telegram_credentials(args.env_files)
        if creds is None:
            log.error("no Telegram credentials found in %s", args.env_files)
            return 2
        ok = send_telegram(*creds, "✅ Revival Radar scan-health monitor is active "
                                   "(checks every 5 min; silent unless something is wrong).")
        print("telegram test message: " + ("delivered" if ok else "FAILED"))
        return 0 if ok else 1

    findings = evaluate(args, now)
    healthy = not findings
    if args.json:
        print(json.dumps({"healthy": healthy,
                          "findings": [f.__dict__ for f in findings]}))
    else:
        print("revival scan health: OK" if healthy else
              "revival scan health: UNHEALTHY\n" + "\n".join(
                  f"  - {f.condition}: {f.message}" for f in findings))
    if args.no_alert:
        return 0 if healthy else 1

    old = load_state(args.state)
    msgs, new_state = decide(old, findings, now, args.cooldown)
    if msgs:
        creds = telegram_credentials(args.env_files)
        if creds is None:
            log.error("no Telegram credentials found in %s", args.env_files)
            return 2 if healthy else 1
        sent_all = True
        for m in msgs:
            if not send_telegram(*creds, m):
                sent_all = False
        if not sent_all:
            # keep the previous state so the next run retries the alert / recovery notice
            new_state = {"conditions": old.get("conditions", {}), "checked_at": now}
            for name in (f.condition for f in findings):
                c = new_state["conditions"].setdefault(
                    name, {"fails": 0, "last_sent": 0.0, "alerting": False})
                c["fails"] = int(c.get("fails", 0)) + 1
    try:
        save_state(args.state, new_state)
    except OSError as e:
        log.error("cannot write state file: %s", e)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
