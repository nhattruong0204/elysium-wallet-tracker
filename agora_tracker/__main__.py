"""CLI entry point: python -m agora_tracker --config config.yaml"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

import requests
import sqlalchemy as sa
import yaml

from . import cards
from .cards import Links
from .chain import ChainFollower, ScanResult
from .config import Config, load_config, load_watchlist
from .db import Store
from .engine import WEI, Engine
from .leaderboard import HttpLeaderboard, diff_leaderboard, dig, guess_mapping, parse_curl
from .market import AgoraMarket
from .notify import Notifier
from .rpc import RpcClient, RpcError, redact

log = logging.getLogger("agora_tracker")


class Tracker:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        c = cfg.chain
        self.store = Store(chain_id=c.chain_id)
        self.rpc = RpcClient(c.rpc_urls, c.rpc_timeout_s, c.rpc_max_retries, c.rpc_batch_size,
                             expected_chain_id=c.chain_id, max_head_lag=c.max_head_lag)
        self.follower = ChainFollower(self.rpc, self.store, c, int(cfg.alerts.hype_dust * WEI),
                                      set(cfg.alerts.ignore_tokens))
        self.watchlist = load_watchlist(cfg.watchlist_file)
        self.store.sync_manual_watchlist(self.watchlist)
        self.links = Links(cfg.links)
        self.market = AgoraMarket(cfg.market)
        self.engine = Engine(self.store, self.follower.tokens, c, cfg.alerts, self.watchlist,
                             value_unit=cfg.leaderboard.value_unit, links=self.links)
        self.notifier = Notifier(self.store, cfg.telegram, dry_run, renderer=self.render_alert)
        lb = cfg.leaderboard
        self.leaderboard = HttpLeaderboard(lb) if lb.enabled and lb.url else None
        self.tracked: set[str] = set()
        self._warned_idle = False
        self._next_lb = 0.0
        self._next_heads = 0.0
        self._stop = False

    def render_alert(self, row: dict) -> tuple[str, dict | None]:
        """Re-render a stored alert with fresh market data (called by the notifier)."""
        payload = row["payload"]
        token = payload.get("token")
        info = self.market.token(token) if token else None
        out = cards.render(payload, self.links, info, self.market.hype_usd())
        return out if out else (row["text"], None)

    def stop(self, *_):
        log.info("shutdown requested")
        self._stop = True

    def check_chain(self) -> None:
        """Verify every configured endpoint's chain id; quarantine mismatches. Refuse to run
        when none is on the expected chain (wrong endpoint or Elysium re-genesis)."""
        results = self.rpc.verify_all()
        for url, res in results.items():
            (log.info if res == "ok" else log.error)("RPC %s: %s", url, res)
        if not self.rpc.live_urls:
            raise SystemExit(f"no RPC endpoint is on chain {self.cfg.chain.chain_id}; "
                             "refusing to run")
        if not any(r == "ok" for r in results.values()):
            raise RpcError("no RPC endpoint reachable")
        self.check_heads()

    def check_heads(self) -> None:
        self._next_heads = time.monotonic() + self.cfg.chain.head_check_s
        if len(self.rpc.urls) < 2:
            return
        heads = self.rpc.refresh_heads()
        best = max((h for h in heads.values() if h is not None), default=None)
        log.debug("RPC heads: %s", ", ".join(
            f"{redact(u)}={h if h is not None else 'down'}"
            + (f" (-{best - h})" if h is not None and best is not None and best > h else "")
            for u, h in heads.items()))

    def refresh_leaderboard(self) -> None:
        self._next_lb = time.monotonic() + self.cfg.leaderboard.refresh_s
        prev = self.store.top_traders()
        top: set[str] = set(prev)
        lb = self.leaderboard
        if lb is not None:
            try:
                traders = lb.fetch()
                status, complete = "ok", len(traders) >= self.cfg.leaderboard.top_n
                if not complete:
                    log.warning("leaderboard returned %d rows (< top_n %d): pagination or "
                                "a changed endpoint?", len(traders), self.cfg.leaderboard.top_n)
                if prev and len(traders) < len(prev) * 0.5:
                    # A sudden halving is far more likely a broken/partial response than half
                    # the top traders vanishing. Record it, keep the previous set, stay quiet.
                    log.error("leaderboard shrank %d -> %d; snapshot recorded as degraded",
                              len(prev), len(traders))
                    status = "degraded"
                raw = lb.last_payload
                self.store.record_snapshot(
                    traders, source="agora", status=status, complete=complete,
                    row_count=len(_rows(raw, self.cfg.leaderboard.items_path)),
                    request_meta={**lb.last_meta,
                                  "value_unit": self.cfg.leaderboard.value_unit},
                    raw_payload=raw)
                if status == "ok":
                    changes = diff_leaderboard(prev, traders,
                                               rank_change=self.cfg.alerts.rank_change,
                                               pnl_change=self.cfg.alerts.pnl_change,
                                               unit=self.cfg.leaderboard.value_unit)
                    if self.cfg.alerts.leaderboard_alerts and changes:
                        self.engine.leaderboard_alerts(changes)
                    top = {t.address for t in traders}
                    log.info("leaderboard: %d traders, %d changes", len(traders), len(changes))
                else:
                    top |= {t.address for t in traders}
            except (requests.RequestException, ValueError, PermissionError) as e:
                log.error("leaderboard refresh failed, keeping previous set (%d): %s", len(top), e)
                self.store.record_snapshot([], source="agora", status="failed", complete=False,
                                           row_count=0, request_meta={**lb.last_meta,
                                                                      "error": str(e)[:300]})
        if self.cfg.exclude_contracts and top:
            try:
                top = self.follower.filter_eoas(top)
            except RpcError as e:
                log.warning("contract filter skipped: %s", e)
        new_set = top | set(self.watchlist)
        added, removed = new_set - self.tracked, self.tracked - new_set
        if added or removed:
            log.info("tracking %d wallets (+%d / -%d)", len(new_set), len(added), len(removed))
        self.tracked = new_set

    def scan_step(self) -> bool:
        """Process one block range. Returns True if still behind head."""
        # One endpoint for the whole range: head, logs, txs and receipts (see rpc.pinned).
        with self.rpc.pinned():
            result = self._scan_range()
        if result is None:
            return False
        scan, frm, to, head = result
        new = self.engine.apply_range(scan, to)
        if new:
            log.info("blocks %d-%d: %d new events", frm, to, new)
        return to < head

    def _scan_range(self) -> tuple[ScanResult, int, int, int] | None:
        c = self.cfg.chain
        head = self.rpc.block_number() - c.confirmations
        stored = self.store.get_cursor()
        cursor = stored if stored is not None else max(0, head - c.backfill_blocks)
        if stored is None:
            self.store.set_cursor(None, cursor)
            log.info("starting at block %d (head %d, backfill %d)", cursor, head, c.backfill_blocks)
        if cursor >= head:
            return None
        if not self.tracked:
            # Scanning with no wallets would advance the cursor and skip these blocks for
            # every wallet tracked later. Hold position until there is something to track.
            if not self._warned_idle:
                log.warning("no wallets tracked; holding cursor at %d", cursor)
                self._warned_idle = True
            return None
        self._warned_idle = False
        to = min(head, cursor + c.max_block_range)
        return self.follower.scan(cursor + 1, to, self.tracked), cursor + 1, to, head

    def run(self, once: bool = False) -> None:
        if not self.store.acquire_indexer_lock():
            raise SystemExit(f"another indexer already holds the lock for chain "
                             f"{self.cfg.chain.chain_id}; refusing to run a second one")
        self.check_chain()
        self.refresh_leaderboard()
        if not self.tracked:
            log.warning("no wallets tracked: configure leaderboard.url or watchlist_file")
        if not once:
            source = "leaderboard + watchlist" if self.leaderboard else "watchlist only"
            self.store.enqueue_now(
                f"start:{int(time.time())}",
                f"▶️ <b>Tracker started</b> · {len(self.tracked)} wallets ({source}) · "
                f"chain {self.cfg.chain.chain_id} via {redact(self.rpc.url)}")
        backoff = 1.0
        while not self._stop:
            try:
                if not self.store.indexer_lock_alive():
                    # Our DB session dropped, so PostgreSQL released the lock. Another indexer
                    # may have taken over: stop instead of risking two writers.
                    raise SystemExit("lost the indexer lock (database connection dropped)")
                if time.monotonic() >= self._next_lb:
                    self.refresh_leaderboard()
                if time.monotonic() >= self._next_heads:
                    self.check_heads()
                behind = self.scan_step()
                self.notifier.drain()
                self.store.set_kv("heartbeat", str(int(time.time())))
                backoff = 1.0
                if once and not behind:
                    break
                if not behind:
                    self._sleep(self.cfg.chain.poll_interval_s)
            except RpcError as e:
                log.error("RPC error: %s (retry in %.0fs)", e, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception:  # keep a long-running bot alive; the healthcheck catches stalls
                log.exception("unexpected error (retry in %.0fs)", backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
        self.notifier.drain(200)
        self.store.release_indexer_lock()

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while not self._stop and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))


def test_telegram(cfg: Config) -> int:
    """Send one message straight to Telegram (bypassing the outbox) to verify credentials."""
    if not (cfg.telegram.bot_token and cfg.telegram.chat_id):
        print("TG_BOT_TOKEN / TG_CHAT_ID not set")
        return 1
    n = Notifier(None, cfg.telegram)  # type: ignore[arg-type]  # _send does not use the store
    ok, err, _ = n._send("✅ <b>Elysium wallet tracker</b>: Telegram delivery works.")
    print("telegram ok" if ok else f"telegram FAILED: {err}")
    return 0 if ok else 1


def test_card(cfg: Config) -> int:
    """Send the most recent tracked BUY/SELL as a full card with live market data."""
    store = Store(chain_id=cfg.chain.chain_id)
    with store.engine.connect() as c:
        r = c.execute(sa.text(
            "SELECT * FROM trades WHERE chain_id=:c AND kind IN ('BUY','SELL') "
            "AND hype_wei IS NOT NULL ORDER BY block_number DESC, id DESC LIMIT 1"),
            {"c": cfg.chain.chain_id}).mappings().first()
    if r is None:
        print("no BUY/SELL recorded yet")
        return 1
    sym, dec = store.get_token(r["token"]) or (r["token"][:8], 18)
    row = store.trader(r["wallet"]) or {}
    payload = {"type": "trade", "kind": r["kind"], "tx": r["tx_hash"], "block": r["block_number"],
               "ts": int(r["block_time"].timestamp()) if r["block_time"] else None,
               "token": r["token"], "symbol": sym, "decimals": dec, "qty": str(int(r["qty"])),
               "hype_wei": str(int(r["hype_wei"])), "confidence": r["confidence"],
               "hype_method": r["hype_method"], "notes": ["🧪 test card: latest tracked trade"],
               "wallet": r["wallet"], "label": None, "rank": row.get("rank"),
               "pnl": row.get("pnl"), "pnl_unit": cfg.leaderboard.value_unit}
    market, links = AgoraMarket(cfg.market), Links(cfg.links)
    text, kb = cards.render_trade(payload, links, market.token(r["token"]), market.hype_usd())
    ok, err, _ = Notifier(None, cfg.telegram)._send(text, kb)  # type: ignore[arg-type]
    print("card sent" if ok else f"card FAILED: {err}")
    return 0 if ok else 1


def import_curl(source: str) -> None:
    text = sys.stdin.read() if source == "-" else open(source).read()
    spec = parse_curl(text.strip())
    section = {"enabled": True, **spec, "items_path": "", "fields": {}}
    try:
        r = requests.request(spec["method"], spec["url"], params=spec["params"] or None,
                             headers=spec["headers"], timeout=20,
                             **({"json": spec["body"]} if isinstance(spec["body"], (dict, list))
                                else {"data": spec["body"]} if spec["body"] else {}))
        r.raise_for_status()
        guess = guess_mapping(r.json())
        if guess:
            section["items_path"], section["fields"] = guess["items_path"], guess["fields"]
            print(f"# detected {guess['count']} rows; keys: {', '.join(guess['sample_keys'])}",
                  file=sys.stderr)
        else:
            print("# no list of address-bearing objects found; set items_path/fields by hand",
                  file=sys.stderr)
    except (requests.RequestException, ValueError) as e:
        print(f"# could not test the request ({e}); fill items_path/fields by hand",
              file=sys.stderr)
    print(yaml.safe_dump({"leaderboard": section}, sort_keys=False, allow_unicode=True))


def run_discover(page_url: str) -> int:
    from .discover import discover
    hits, tried = discover(page_url)
    if not hits:
        print(f"# no JSON trader list found among {len(tried)} candidates:", file=sys.stderr)
        for u in tried[:40]:
            print(f"#   {u}", file=sys.stderr)
        print("# fall back to: --import-curl (DevTools 'Copy as cURL') or a watchlist",
              file=sys.stderr)
        return 1
    best = hits[0]
    for h in hits:
        print(f"# {h['count']:>4} rows  {h['url']}  fields={h['fields']}", file=sys.stderr)
    section = {"enabled": True, "url": best["url"], "method": "GET", "params": best["params"],
               "headers": {"accept": "application/json",
                           "referer": "https://www.agoradex.app/traders"},
               "body": None, "items_path": best["items_path"], "fields": best["fields"],
               "sort_by": "pnl" if "pnl" in best["fields"] else "", "top_n": 100,
               "refresh_s": 300}
    print(yaml.safe_dump({"leaderboard": section}, sort_keys=False))
    return 0


def _rows(payload, items_path: str) -> list:
    rows = dig(payload, items_path) if payload is not None else None
    return rows if isinstance(rows, list) else []


def health(max_age_s: int) -> int:
    """Exit 0 when the database is reachable, migrations are at head and the main loop
    wrote a heartbeat recently (Docker HEALTHCHECK)."""
    from .db import make_engine
    from .migrate import current_revision, head_revision
    try:
        engine = make_engine(pool_size=1)
        rev, head = current_revision(engine), head_revision()
        if rev != head:
            print(f"unhealthy: schema at {rev}, code expects {head} (run --migrate)")
            return 1
        with engine.connect() as c:
            hb = c.execute(sa.text(
                "SELECT value->>'v' FROM service_state WHERE key='heartbeat'")).scalar()
    except Exception as e:  # database down, auth, DNS ...
        print(f"unhealthy: database: {type(e).__name__}: {str(e)[:200]}")
        return 1
    age = time.time() - int(hb) if hb else None
    if age is None or age > max_age_s:
        print(f"unhealthy: heartbeat age {age}")
        return 1
    print(f"ok: heartbeat {age:.0f}s ago, schema {rev}")
    return 0


def top(cfg: Config, n: int) -> None:
    """Latest accepted leaderboard snapshot, best first, values in the source's unit."""
    unit = cfg.leaderboard.value_unit
    rows = sorted(Store(chain_id=cfg.chain.chain_id).top_traders().items(),
                  key=lambda kv: kv[1]["rank"])[:n]
    if not rows:
        print("no leaderboard snapshot yet")
        return
    print(f"{'#':>3}  {'wallet':<42}  {'PnL (' + unit + ')':>14}")
    for addr, r in rows:
        pnl = "-" if r["pnl"] is None else f"{r['pnl']:,.2f}"
        print(f"{r['rank']:>3}  {addr:<42}  {pnl:>14}")


def status(cfg: Config) -> None:
    print(json.dumps(Store(chain_id=cfg.chain.chain_id).status(), indent=2, default=str))


def migrate() -> int:
    from .db import make_engine
    from .migrate import current_revision, head_revision, upgrade
    engine = make_engine(pool_size=1)
    before = current_revision(engine)
    upgrade(engine)
    log.info("schema %s -> %s", before, head_revision())
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agora_tracker",
                                 description="Follow top Elysium traders and alert on their trades.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="catch up to head once, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print alerts instead of Telegram; with --import-sqlite: validate "
                         "and roll back")
    ap.add_argument("--import-curl", metavar="FILE",
                    help="build the leaderboard config from a DevTools 'Copy as cURL' ('-' = stdin)")
    ap.add_argument("--status", action="store_true", help="print state summary and exit")
    ap.add_argument("--discover", nargs="?", const="https://www.agoradex.app/traders",
                    metavar="PAGE_URL",
                    help="find the leaderboard API from the site's JS and print a config block")
    ap.add_argument("--health", action="store_true",
                    help="exit 0 if the bot loop is alive (Docker healthcheck)")
    ap.add_argument("--health-max-age", type=int, default=600, metavar="SECONDS")
    ap.add_argument("--reset-cursor", type=int, metavar="BLOCK",
                    help="set the last-processed block (replay is safe: events are deduplicated)")
    ap.add_argument("--migrate", action="store_true",
                    help="apply database migrations (DATABASE_URL) and exit")
    ap.add_argument("--import-sqlite", metavar="PATH",
                    help="one-time import of a v0.1/v0.2 SQLite database into PostgreSQL")
    ap.add_argument("--report", metavar="FILE", help="write the import reconciliation report "
                                                     "as JSON (with --import-sqlite)")
    ap.add_argument("--top", type=int, nargs="?", const=10, metavar="N",
                    help="print the top N wallets of the latest leaderboard snapshot")
    ap.add_argument("--test-card", action="store_true",
                    help="send the latest tracked trade to Telegram as a full card")
    ap.add_argument("--test-telegram", action="store_true",
                    help="send one test message to Telegram and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.log_level.upper() != "DEBUG":
        logging.getLogger("alembic").setLevel(logging.WARNING)

    if args.import_curl:
        import_curl(args.import_curl)
        return
    if args.discover:
        try:
            sys.exit(run_discover(args.discover))
        except RuntimeError as e:
            log.error("discover failed: %s", e)
            sys.exit(1)
    if args.health:
        sys.exit(health(args.health_max_age))
    if args.migrate:
        sys.exit(migrate())
    cfg = load_config(args.config)
    if args.test_telegram:
        sys.exit(test_telegram(cfg))
    if args.test_card:
        sys.exit(test_card(cfg))
    if args.import_sqlite:
        from .sqlite_import import run_import
        sys.exit(run_import(args.import_sqlite, cfg, dry_run=args.dry_run,
                            report_path=args.report))
    if args.status:
        status(cfg)
        return
    if args.top:
        top(cfg, args.top)
        return
    if args.reset_cursor is not None:
        Store(chain_id=cfg.chain.chain_id).set_cursor(None, args.reset_cursor)
        log.info("cursor set to %d", args.reset_cursor)
        return

    t = Tracker(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGINT, t.stop)
    signal.signal(signal.SIGTERM, t.stop)
    try:
        t.run(once=args.once)
    except RpcError as e:
        # Startup RPC failure: exit non-zero and let Docker's restart policy retry.
        log.error("cannot reach Elysium RPC at startup: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
