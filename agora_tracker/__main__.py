"""CLI entry point: python -m agora_tracker --config config.yaml"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

import requests
import yaml

from .chain import ChainFollower
from .config import Config, load_config, load_watchlist
from .db import Store
from .engine import WEI, Engine
from .leaderboard import HttpLeaderboard, diff_leaderboard, guess_mapping, parse_curl
from .notify import Notifier
from .rpc import RpcClient, RpcError

log = logging.getLogger("agora_tracker")


class Tracker:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        c = cfg.chain
        self.rpc = RpcClient(c.rpc_urls, c.rpc_timeout_s, c.rpc_max_retries, c.rpc_batch_size)
        self.follower = ChainFollower(self.rpc, self.store, c, int(cfg.alerts.hype_dust * WEI),
                                      set(cfg.alerts.ignore_tokens))
        self.watchlist = load_watchlist(cfg.watchlist_file)
        self.engine = Engine(self.store, self.follower.tokens, c, cfg.alerts, self.watchlist)
        self.notifier = Notifier(self.store, cfg.telegram, dry_run)
        lb = cfg.leaderboard
        self.leaderboard = HttpLeaderboard(lb) if lb.enabled and lb.url else None
        self.tracked: set[str] = set()
        self._next_lb = 0.0
        self._stop = False

    def stop(self, *_):
        log.info("shutdown requested")
        self._stop = True

    def check_chain(self) -> None:
        cid = self.rpc.chain_id()
        if cid != self.cfg.chain.chain_id:
            raise SystemExit(f"RPC chain id {cid} != configured {self.cfg.chain.chain_id}; "
                             "wrong endpoint or Elysium re-genesis. Refusing to run.")
        log.info("connected to chain %d via %s", cid, self.rpc.url)

    def refresh_leaderboard(self) -> None:
        self._next_lb = time.monotonic() + self.cfg.leaderboard.refresh_s
        top: set[str] = set(self.store.top_traders())
        if self.leaderboard:
            try:
                traders = self.leaderboard.fetch()
                prev = self.store.top_traders()
                changes = diff_leaderboard(prev, traders, rank_change=self.cfg.alerts.rank_change,
                                           pnl_change_usd=self.cfg.alerts.pnl_change_usd)
                self.store.replace_top(traders)
                if self.cfg.alerts.leaderboard_alerts and changes:
                    self.engine.leaderboard_alerts(changes)
                top = {t.address for t in traders}
                log.info("leaderboard: %d traders, %d changes", len(traders), len(changes))
            except (requests.RequestException, ValueError, PermissionError) as e:
                log.error("leaderboard refresh failed, keeping previous set (%d): %s", len(top), e)
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
        c = self.cfg.chain
        head = self.rpc.block_number() - c.confirmations
        raw = self.store.get_kv("cursor")
        cursor = int(raw) if raw is not None else max(0, head - c.backfill_blocks)
        if raw is None:
            self.store.set_kv("cursor", str(cursor))
            log.info("starting at block %d (head %d, backfill %d)", cursor, head, c.backfill_blocks)
        if cursor >= head:
            return False
        to = min(head, cursor + c.max_block_range)
        events, ts = self.follower.scan(cursor + 1, to, self.tracked)
        new = self.engine.apply_range(events, ts, to)
        if new:
            log.info("blocks %d-%d: %d new events", cursor + 1, to, new)
        return to < head

    def run(self, once: bool = False) -> None:
        self.check_chain()
        self.refresh_leaderboard()
        if not self.tracked:
            log.warning("no wallets tracked: configure leaderboard.url or watchlist_file")
        if not once:
            source = "leaderboard + watchlist" if self.leaderboard else "watchlist only"
            self.store.enqueue_now(
                f"start:{int(time.time())}",
                f"▶️ <b>Tracker started</b> · {len(self.tracked)} wallets ({source}) · "
                f"chain {self.cfg.chain.chain_id} via {self.rpc.url}")
        backoff = 1.0
        while not self._stop:
            try:
                if time.monotonic() >= self._next_lb:
                    self.refresh_leaderboard()
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

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while not self._stop and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))


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


def health(cfg: Config, max_age_s: int) -> int:
    """Exit 0 if the main loop wrote a heartbeat recently (for Docker HEALTHCHECK)."""
    try:
        hb = Store(cfg.db_path).get_kv("heartbeat")
    except Exception as e:  # db missing or locked
        print(f"unhealthy: {e}")
        return 1
    age = time.time() - int(hb) if hb else None
    if age is None or age > max_age_s:
        print(f"unhealthy: heartbeat age {age}")
        return 1
    print(f"ok: heartbeat {age:.0f}s ago")
    return 0


def status(cfg: Config) -> None:
    store = Store(cfg.db_path)
    top = store.top_traders()
    print(json.dumps({
        "cursor": store.get_kv("cursor"),
        "top_traders": len(top),
        "watchlist": len(load_watchlist(cfg.watchlist_file)),
        "pending_alerts": len(store.pending_alerts(1000)),
        "events": store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "open_positions": store.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE qty != '0'").fetchone()[0],
    }, indent=2))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agora_tracker",
                                 description="Follow top Elysium traders and alert on their trades.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="catch up to head once, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of Telegram")
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
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.import_curl:
        import_curl(args.import_curl)
        return
    if args.discover:
        try:
            sys.exit(run_discover(args.discover))
        except RuntimeError as e:
            log.error("discover failed: %s", e)
            sys.exit(1)
    cfg = load_config(args.config)
    if args.health:
        sys.exit(health(cfg, args.health_max_age))
    if args.status:
        status(cfg)
        return
    if args.reset_cursor is not None:
        Store(cfg.db_path).set_kv("cursor", str(args.reset_cursor))
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
