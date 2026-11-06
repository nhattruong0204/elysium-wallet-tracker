"""CLI entry point: python -m agora_tracker --config config.yaml"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
import sqlalchemy as sa
import yaml

from . import cards
from . import schema as tables
from .cards import Links
from .chain import ChainFollower, ScanResult
from .config import Config, load_config, load_watchlist
from .db import Store
from .engine import WEI, Engine
from .launchintel import AscendIndexer, DueDiligence, LaunchWatcher
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
                             value_unit=cfg.leaderboard.value_unit, links=self.links,
                             intel=cfg.intel)
        ic = cfg.intel
        self.dd = DueDiligence(
            self.rpc, wrapped=ic.wrapped_native, factories=ic.dd_factories,
            budget_s=ic.dd_budget_s, cache_s=ic.dd_cache_s, log_blocks=ic.dd_log_blocks,
            block_time_s=ic.dd_block_time_s, ascend_lookup=self.store.ascend_launch,
        ) if ic.dd_enabled else None
        self.ascend = AscendIndexer(self.store, self.rpc, max_chunks=ic.ascend_max_chunks) \
            if ic.ascend_index else None
        self._next_ascend = 0.0
        self.launch = LaunchWatcher(
            self.store, self.rpc, wrapped=ic.wrapped_native, factories=ic.launch_factories,
            min_liquidity_hype=ic.launch_min_liquidity_hype, delay_s=ic.launch_delay_s,
        ) if ic.launch_watch else None
        self._next_launch = 0.0
        self.notifier = Notifier(self.store, cfg.telegram, dry_run, renderer=self.render_alert)
        lb = cfg.leaderboard
        self.leaderboard = HttpLeaderboard(lb) if lb.enabled and lb.url else None
        self.tracked: set[str] = set()
        self.tracked_leaderboard: set[str] = set()   # top | ever-in-top, no manual/Telegram
        self._covered: set[str] = set()      # the set wallet_scan_coverage currently reflects
        self._warned_capacity = False
        self._ema_s = 0.0
        self._warned_idle = False
        self._next_lb = 0.0
        self._next_heads = 0.0
        self._next_disk = 0.0
        self._stop = False

    def render_alert(self, row: dict) -> tuple[str, dict | None, str | None]:
        """Re-render a stored alert with fresh market data (called by the notifier)."""
        payload = row["payload"]
        token = payload.get("token")
        info = self.market.token(token) if token else None
        dd = None
        if self.dd is not None and token and cards.wants_dd(payload):
            try:   # bounded, cached, optional: the alert goes out with or without it
                dd = self.dd.probe(token, payload.get("tx"))
            except Exception:  # noqa: BLE001
                log.warning("due diligence probe failed for %s", token, exc_info=True)
        if info is not None and getattr(dd, "venue", None) == "ascend":
            info = dataclasses.replace(info, launchpad="ascend")   # chain fact beats Agora's label
        out = cards.render(payload, self.links, info, self.market.hype_usd(), dd=dd)
        if not out:
            return row["text"], None, None
        img = cards.preview_url(self.links, info) if self.cfg.alerts.token_image else None
        return out[0], out[1], img

    def run_ascend_index(self) -> None:
        """Ascend launches from the hook's events. Isolated: never touches indexing."""
        if self.ascend is None or time.monotonic() < self._next_ascend:
            return
        self._next_ascend = time.monotonic() + self.cfg.intel.ascend_interval_s
        try:
            n = self.ascend.tick()
            if n:
                log.info("ascend: %d new launches indexed", n)
        except Exception as e:  # noqa: BLE001 - e.g. a log-search rate limit: retry next tick
            log.warning("ascend indexer: %s", str(e)[:200])

    def run_launch_watch(self) -> None:
        """New DEX pools from chain events. Isolated: a failure here never touches indexing."""
        if self.launch is None or time.monotonic() < self._next_launch:
            return
        self._next_launch = time.monotonic() + self.cfg.intel.launch_watch_interval_s
        try:
            self.launch.tick()
            if self.dd is not None:
                self.dd.add_factories(self.store.launch_factories())
        except Exception as e:  # noqa: BLE001
            log.warning("launch watcher: %s", e)

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

    def check_disk(self) -> None:
        """The container shares the host filesystem: warn on Telegram when it runs low.
        One alert per 6 h while the condition lasts; usage is also recorded for /health."""
        self._next_disk = time.monotonic() + 600
        try:
            du = shutil.disk_usage("/")
        except OSError as e:
            log.warning("disk check failed: %s", e)
            return
        free_pct = du.free / du.total
        try:
            self.store.set_kv("disk", json.dumps({"at": int(time.time()), "total": du.total,
                                                  "free": du.free}))
        except Exception as e:  # noqa: BLE001
            log.debug("recording disk usage failed: %s", e)
        a = self.cfg.alerts
        if free_pct < a.disk_free_pct or du.free < a.disk_free_gb * 1e9:
            self.store.enqueue_now(
                f"disk:{int(time.time()) // 21600}",
                f"⚠️ <b>VPS disk low</b>: {du.free / 1e9:.1f} GB free of {du.total / 1e9:.0f} GB "
                f"({free_pct:.0%}). Check backups, logs and Docker build cache before databases "
                "fill up.", severity="high")
            log.warning("disk low: %.1f GB free (%.0f%%)", du.free / 1e9, free_pct * 100)

    def check_heads(self) -> None:
        self._next_heads = time.monotonic() + self.cfg.chain.head_check_s
        heads = self.rpc.refresh_heads()
        try:  # for the Telegram health page; hosts only (URLs may embed API keys)
            best = max((h for h in heads.values() if h is not None), default=None)
            self.store.set_kv("rpc_heads", json.dumps({
                "at": int(time.time()), "best": best,
                "endpoints": [{"host": urlsplit(u).hostname, "head": h,
                               "lagging": u in self.rpc._lagging} for u, h in heads.items()]}))
        except Exception as e:  # noqa: BLE001
            log.debug("recording RPC heads failed: %s", e)
        best = max((h for h in heads.values() if h is not None), default=None)
        log.debug("RPC heads: %s", ", ".join(
            f"{redact(u)}={h if h is not None else 'down'}"
            + (f" (-{best - h})" if h is not None and best is not None and best > h else "")
            for u, h in heads.items()))

    def prepare_registry(self) -> None:
        """Start-up, before the first tracking set is built: rebuild the wallet registry from
        stored snapshots if it is behind (idempotent) and reconstruct scan coverage for what
        was scanned before coverage was recorded. Never blocks start-up."""
        try:
            rebuilt = self.store.ensure_enrollment()
            if rebuilt:
                log.info("wallet registry rebuilt from stored snapshots: %s", rebuilt)
            n = self.store.derive_legacy_coverage()
            if n:
                log.info("reconstructed %d legacy scan-coverage intervals (approximate)", n)
        except Exception:  # noqa: BLE001
            log.exception("wallet registry preparation failed; continuing with the "
                          "snapshot-based tracking set")

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
        # Cumulative tracking: everyone ever in an accepted, complete top list stays in the set
        # (leaving the list changes membership, not eligibility). A failed or degraded
        # snapshot adds nothing and removes nothing: the registry lives in the database.
        hist: set[str] = set()
        if self.cfg.tracking.cumulative:
            try:
                hist = self.store.enrolled_addresses()
            except Exception as e:  # noqa: BLE001 - a DB hiccup must never shrink the scan set
                log.warning("enrollment registry unavailable, keeping the current set: %s", e)
                hist = set(self.tracked_leaderboard)
        universe = top | hist
        if self.cfg.exclude_contracts and universe:
            try:
                kept = self.follower.filter_eoas(universe)
                top, hist = top & kept, hist & kept
            except RpcError as e:
                log.warning("contract filter skipped: %s", e)
            try:
                self.store.sync_exclusions(True)
            except Exception as e:  # noqa: BLE001
                log.warning("recording contract exclusions failed: %s", e)
        try:
            via_bot = self.store.watchlist_addresses("telegram")
        except Exception as e:  # noqa: BLE001 - never let the bot's list block tracking
            log.warning("telegram watchlist unavailable: %s", e)
            via_bot = set()
        self.tracked_leaderboard = top | hist
        new_set = top | hist | set(self.watchlist) | via_bot
        added, removed = new_set - self.tracked, self.tracked - new_set
        if added or removed:
            log.info("tracking %d wallets (+%d / -%d): %d current top, %d ever in top, "
                     "%d manual, %d telegram", len(new_set), len(added), len(removed),
                     len(top), len(hist), len(self.watchlist), len(via_bot))
        cap = self.cfg.tracking.capacity_warn
        if len(new_set) > cap and not self._warned_capacity:
            log.warning("tracking %d wallets, above the configured capacity %d: scan cost grows "
                        "linearly with wallets; watch scan_stats / lag and see docs/OPERATIONS.md",
                        len(new_set), cap)
            self._warned_capacity = True
        elif len(new_set) <= cap:
            self._warned_capacity = False
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
        self._sync_coverage(cursor)
        to = min(head, cursor + self.follower.next_span(len(self.tracked)))
        m0, t0 = dict(getattr(self.rpc, "metrics", None) or {}), time.monotonic()
        scan = self.follower.scan(cursor + 1, to, self.tracked)
        self._record_scan_stats(m0, t0, cursor + 1, to, head)
        return scan, cursor + 1, to, head

    def _sync_coverage(self, cursor: int) -> None:
        """Record which wallets this range really scans (opened at cursor+1, closed at the
        cursor when a wallet leaves the set). Metadata only: a failure never blocks scanning,
        it only makes the coverage report more conservative."""
        if self.tracked == self._covered:
            return
        try:
            opened, closed = self.store.sync_coverage(self.tracked, cursor)
            self._covered = set(self.tracked)
            if opened or closed:
                log.info("scan coverage: +%d wallets from block %d, -%d", opened, cursor + 1,
                         closed)
        except Exception as e:  # noqa: BLE001
            log.warning("recording scan coverage failed (will retry): %s", e)

    def _record_scan_stats(self, m0: dict, t0: float, frm: int, to: int, head: int) -> None:
        """Price of the last scanned range: HTTP requests, getLogs calls, wall time, lag. The
        public dashboard reads the key; it holds aggregates only, never wallet identities."""
        try:
            m1, dt = getattr(self.rpc, "metrics", None) or {}, time.monotonic() - t0
            blocks = to - frm + 1
            per_1k = dt / blocks * 1000
            self._ema_s = per_1k if not self._ema_s else 0.8 * self._ema_s + 0.2 * per_1k
            n = len(self.tracked_leaderboard)
            self.store.set_kv("scan_stats", json.dumps({
                "at": int(time.time()), "from_block": frm, "to_block": to, "blocks": blocks,
                "head": head, "lag_blocks": head - to,
                "http_requests": int(m1.get("http", 0) - m0.get("http", 0)),
                "rpc_calls": int(m1.get("calls", 0) - m0.get("calls", 0)),
                "get_logs": int(m1.get("m:eth_getLogs", 0) - m0.get("m:eth_getLogs", 0)),
                "rpc_seconds": round(m1.get("seconds", 0) - m0.get("seconds", 0), 2),
                "scan_seconds": round(dt, 2), "seconds_per_1k_blocks": round(per_1k, 2),
                "ema_seconds_per_1k_blocks": round(self._ema_s, 2),
                "scan_mode": "full" if self.follower.uses_full_scan(len(self.tracked))
                else "filtered",
                "chain_logs_per_block": round(self.follower.last_density, 3),
                "wallet_chunk": self.follower.wallet_chunk(),
                "wallets_leaderboard": n,
                "capacity_warn": self.cfg.tracking.capacity_warn,
                "over_capacity": len(self.tracked) > self.cfg.tracking.capacity_warn,
                "falling_behind": head - to > self.cfg.tracking.lag_warn_blocks}))
        except Exception as e:  # noqa: BLE001 - instrumentation must never stop the scanner
            log.debug("recording scan stats failed: %s", e)

    def run(self, once: bool = False) -> None:
        if not self.store.acquire_indexer_lock():
            raise SystemExit(f"another indexer already holds the lock for chain "
                             f"{self.cfg.chain.chain_id}; refusing to run a second one")
        self.check_chain()
        self.prepare_registry()
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
                if time.monotonic() >= self._next_disk:
                    self.check_disk()
                if time.monotonic() >= self._next_heads:
                    self.check_heads()
                behind = self.scan_step()
                self.run_launch_watch()
                self.run_ascend_index()
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


def launch_report(cfg: Config) -> None:
    """Paper-trading results and what the launch watchers have seen (read-only, no RPC)."""
    from .launchintel import format_paper_report, summarize_paper
    store = Store(chain_id=cfg.chain.chain_id)
    rep = summarize_paper(store.paper_trades(), time.time(), cfg.intel.paper_slippage_pct)
    print(format_paper_report(rep))
    for r in rep["rows"]:
        when = datetime.fromtimestamp(r["opened_ts"], timezone.utc).strftime("%m-%d %H:%M")
        peak = f"{r['peak']:.2f}x" if r["priced"] else "unpriced"
        last = f"{r['last']:.2f}x" if r["priced"] else "-"
        print(f"  {when} {r['token']} indep={r['n_independent']} peak={peak} now={last}")
    lv = tables.launch_venues
    with store.engine.connect() as c:
        for kind, n, newest in c.execute(sa.select(
                lv.c.kind, sa.func.count(), sa.func.max(lv.c.first_seen_at))
                .where(lv.c.chain_id == store.chain_id).group_by(lv.c.kind)):
            print(f"launch venues: {kind}: {n} seen, newest {newest:%Y-%m-%d %H:%M} UTC")
        pend = c.execute(sa.select(sa.func.count()).where(
            lv.c.chain_id == store.chain_id, lv.c.kind == "pool",
            lv.c.alerted_at.is_(None))).scalar()
    print(f"pools waiting for liquidity/delay: {pend}; watcher cursor: "
          f"{store.get_kv('launch_watch_cursor')}")


def tracking_report(cfg: Config, backfill: bool = False) -> None:
    """Reconciliation of the tracked-wallet universe (counts only, no addresses, no RPC).
    With --tracking-backfill the registry and legacy coverage are rebuilt first (idempotent)."""
    store = Store(chain_id=cfg.chain.chain_id)
    if backfill:
        print("registry:", store.ensure_enrollment() or "already up to date")
        print("legacy coverage intervals written:", store.derive_legacy_coverage())
    rep = store.tracking_report()
    for k, v in rep.items():
        print(f"{k}: {v}")
    raw = store.get_kv("scan_stats")
    if raw:
        print("scan_stats:", raw)


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
               "pnl": row.get("pnl"), "pnl_unit": cfg.leaderboard.value_unit, "v": 2}
    with store.engine.connect() as c:
        pos = c.execute(sa.text(
            "SELECT qty, cost_wei, basis FROM wallet_positions WHERE chain_id = :c "
            "AND wallet = :w AND token = :t"), {"c": cfg.chain.chain_id, "w": r["wallet"],
                                                 "t": r["token"]}).first()
    if pos is not None:   # current position (the real alert carries the post-trade one)
        payload.update(pos_qty=str(int(pos[0])), pos_cost_wei=str(int(pos[1])),
                       pos_basis=pos[2])
    market, links = AgoraMarket(cfg.market), Links(cfg.links)
    info = market.token(r["token"])
    text, kb = cards.render_trade(payload, links, info, market.hype_usd())
    text = "🧪 <i>Test card: latest tracked trade, re-rendered now</i>\n\n" + text
    img = cards.preview_url(links, info) if cfg.alerts.token_image else None
    ok, err, _ = Notifier(None, cfg.telegram)._send(text, kb, img)  # type: ignore[arg-type]
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


def health(max_age_s: int, key: str = "heartbeat") -> int:
    """Exit 0 when the database is reachable, migrations are at head and the main loop
    wrote a heartbeat recently (Docker HEALTHCHECK). key='ui_heartbeat' for the UI."""
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
                "SELECT value->>'v' FROM service_state WHERE key = :k"), {"k": key}).scalar()
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
    pw = os.environ.get("DASHBOARD_DB_PASSWORD")
    if pw:
        from .migrate import set_dashboard_password
        set_dashboard_password(engine, pw)
        log.info("dashboard_ro password set from DASHBOARD_DB_PASSWORD")
    return 0


def run_ui(cfg: Config) -> None:
    """The Telegram Intelligence Center service: update consumer + market collector."""
    import threading

    from .collector import Collector
    from .db import make_engine
    from .migrate import current_revision, head_revision
    from .reports import Reports
    from .tgui import IntelBot, TelegramApi

    if not cfg.telegram.bot_token:
        raise SystemExit("TG_BOT_TOKEN is not set")
    engine = make_engine(pool_size=4)
    rev, head = current_revision(engine), head_revision()
    if rev != head:
        raise SystemExit(f"schema at {rev}, code expects {head}: run --migrate first")
    store = Store(engine, chain_id=cfg.chain.chain_id)
    market = AgoraMarket(cfg.market)
    reports = Reports(engine, cfg.chain.chain_id, cfg.ui, value_unit=cfg.leaderboard.value_unit,
                      market=market, collector_interval_s=cfg.collector.interval_s)
    bot = IntelBot(cfg, TelegramApi(cfg.telegram.bot_token, cfg.telegram.timeout_s), store,
                   reports, market)
    stop = threading.Event()

    def _stop(*_: object) -> None:
        log.info("shutdown requested")
        stop.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if cfg.collector.enabled:
        coll = Collector(store, AgoraMarket(cfg.market), cfg.collector,
                         new_launchpad_alerts=cfg.intel.new_launchpad_alerts)
        threading.Thread(target=coll.loop, args=(stop,), name="collector", daemon=True).start()
    if cfg.scoring.enabled:
        from . import scoring
        threading.Thread(target=scoring.loop, args=(store, cfg.scoring, stop), name="scoring",
                         daemon=True).start()
    if cfg.clustering.enabled:
        from . import clustering
        threading.Thread(target=clustering.loop, args=(store, cfg.clustering, stop),
                         name="clustering", daemon=True).start()
    try:
        bot.run(stop)
    finally:
        bot.shutdown()
        engine.dispose()


def run_ui_smoke(cfg: Config) -> int:
    from .db import make_engine
    from .reports import Reports
    from .tgui import IntelBot, TelegramApi, smoke

    if not (cfg.telegram.bot_token and cfg.ui.admin_ids):
        print("TG_BOT_TOKEN and TG_CHAT_ID are required")
        return 1
    engine = make_engine(pool_size=2)
    store = Store(engine, chain_id=cfg.chain.chain_id)
    market = AgoraMarket(cfg.market)
    reports = Reports(engine, cfg.chain.chain_id, cfg.ui, value_unit=cfg.leaderboard.value_unit,
                      market=market, collector_interval_s=cfg.collector.interval_s)
    bot = IntelBot(cfg, TelegramApi(cfg.telegram.bot_token), store, reports, market)
    try:
        out = smoke(bot, cfg.ui.admin_ids[0])
    finally:
        bot.shutdown()
    for data, res, ms in out["results"]:
        print(f"{'OK ' if res.startswith('ok') else 'BAD'} {ms:>6} ms  {data[:60]}  {res[:120]}")
    print(f"{len(out['results']) - len(out['failed'])}/{len(out['results'])} checks passed")
    return 1 if out["failed"] else 0


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
    ap.add_argument("--ui", action="store_true",
                    help="run the Telegram Intelligence Center (menu, reports, exports) and "
                         "the market snapshot collector")
    ap.add_argument("--health-ui", action="store_true",
                    help="exit 0 if the Telegram UI loop is alive (Docker healthcheck)")
    ap.add_argument("--ui-smoke", action="store_true",
                    help="end-to-end check of the Telegram UI on live data; sends the result "
                         "and a sample XLSX to the owner chat (does not poll updates)")
    ap.add_argument("--web", action="store_true",
                    help="serve the public research dashboard (read-only database role)")
    ap.add_argument("--host", default=None, help="--web bind address (default web.host)")
    ap.add_argument("--port", type=int, default=None, help="--web port (default web.port)")
    ap.add_argument("--launch-report", action="store_true",
                    help="print paper-trading results and launch-watcher state, then exit")
    ap.add_argument("--tracking-report", action="store_true",
                    help="reconcile the tracked-wallet universe (counts only), then exit")
    ap.add_argument("--cluster-now", action="store_true",
                    help="run the wallet-cluster evaluation for the current interval and exit")
    ap.add_argument("--cluster-backfill", type=int, metavar="DAYS",
                    help="replay point-in-time cluster runs at 00:00 UTC for the last DAYS days "
                         "(only forward from the latest existing run)")
    ap.add_argument("--score-now", action="store_true",
                    help="compute the wallet scores for the current hour and exit")
    ap.add_argument("--score-backfill", type=int, metavar="DAYS",
                    help="replay point-in-time scores at 00:00 UTC for the last DAYS days")
    ap.add_argument("--tracking-backfill", action="store_true",
                    help="rebuild the wallet registry from stored snapshots (idempotent), "
                         "then print the reconciliation")
    ap.add_argument("--collect-once", action="store_true",
                    help="take one market snapshot round and exit")
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
    if args.health_ui:
        sys.exit(health(min(args.health_max_age, 180), key="ui_heartbeat"))
    if args.migrate:
        sys.exit(migrate())
    cfg = load_config(args.config)
    if args.test_telegram:
        sys.exit(test_telegram(cfg))
    if args.test_card:
        sys.exit(test_card(cfg))
    if args.web:
        from .web.run import serve
        serve(cfg, args.host, args.port)
        return
    if args.ui:
        run_ui(cfg)
        return
    if args.ui_smoke:
        sys.exit(run_ui_smoke(cfg))
    if args.collect_once:
        from .collector import Collector
        out = Collector(Store(chain_id=cfg.chain.chain_id), AgoraMarket(cfg.market),
                        cfg.collector).run_once()
        print(json.dumps(out))
        sys.exit(0 if out.get("ok") else 1)
    if args.import_sqlite:
        from .sqlite_import import run_import
        sys.exit(run_import(args.import_sqlite, cfg, dry_run=args.dry_run,
                            report_path=args.report))
    if args.status:
        status(cfg)
        return
    if args.launch_report:
        launch_report(cfg)
        return
    if args.cluster_now or args.cluster_backfill is not None:
        from . import clustering
        st = Store(chain_id=cfg.chain.chain_id)
        out = clustering.run_once(st, cfg.clustering) if args.cluster_now else \
            clustering.backfill(st, cfg.clustering, args.cluster_backfill)
        print(json.dumps(out))
        return
    if args.score_now or args.score_backfill is not None:
        from . import scoring
        st = Store(chain_id=cfg.chain.chain_id)
        out = scoring.run_once(st, cfg.scoring) if args.score_now else \
            scoring.backfill(st, cfg.scoring, args.score_backfill)
        print(json.dumps(out))
        return
    if args.tracking_report or args.tracking_backfill:
        tracking_report(cfg, backfill=args.tracking_backfill)
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
