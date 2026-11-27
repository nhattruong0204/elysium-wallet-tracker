"""Trader performance badges: who is buying, and how have they done lately?

Every alert card for a wallet carries its recent record so a reader can decide in a glance
whether the trade is worth tailing (owner request, 2026-11-27). The ranking is our own and
deliberately narrow, so a badge means "followable profit", not "big number":

* realized PnL that we **measured** (cost known, status 'measured') on sells in the last
  `window_days` (30), in HYPE;
* without wallets that are never alerted anyway (launch cohorts, serial launchers, muted) and
  without tokens nobody can follow (launcher-made tokens, muted/hidden tokens);
* without positions the wallet opened within `snipe_s` (60 s) of the token's launch: a launch
  snipe is not an entry a human can copy. Review of 2026-11-27: the two wallets on top of the
  raw 30-day list made 99% (2,737 of 2,767 HYPE) and 96% of their PnL that way;
* ranked when the wallet closed >= `min_closes` trades on >= `min_tokens` tokens and is up.

Tiers: #1-10 ELITE, #11-30 PRO, #31-100 TOP 100. Unranked wallets still get their numbers
(losing streaks are a warning too). Unqualified table names: runs on `public` (tracker).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

WEI = 10**18
WEEKS = 4                      # weekly results shown oldest -> newest

# (last rank of the tier, key, badge, title)
TIERS = ((10, "elite", "👑", "ELITE"), (30, "pro", "💎", "PRO"), (100, "top100", "🏅", "TOP 100"))

PERF_SQL = """
WITH r AS (
    SELECT r.wallet, r.token, r.pnl_wei, t.block_time AS bt
      FROM realized_pnl r JOIN trades t ON t.id = r.trade_id
     WHERE r.chain_id = :c AND r.status = 'measured' AND r.pnl_wei IS NOT NULL
       AND t.block_time >= :since
       AND NOT (r.token = ANY(CAST(:skip_tokens AS text[])))
       AND NOT (r.wallet = ANY(CAST(:skip_wallets AS text[])))),
fb AS (
    SELECT b.wallet, b.token, min(b.block_time) AS fb FROM trades b
     WHERE b.chain_id = :c AND b.kind = 'BUY'
       AND (b.wallet, b.token) IN (SELECT DISTINCT wallet, token FROM r)
     GROUP BY b.wallet, b.token),
snipe AS (
    SELECT fb.wallet, fb.token FROM fb
      JOIN token_profiles p ON p.chain_id = :c AND p.address = fb.token
     WHERE p.launched_at IS NOT NULL AND fb.fb < p.launched_at + make_interval(secs => :snipe_s))
SELECT r.wallet,
       sum(r.pnl_wei) AS pnl,
       coalesce(sum(r.pnl_wei) FILTER (WHERE r.bt >= :w1), 0) AS pnl_7d,
       count(*) AS closes,
       count(*) FILTER (WHERE r.pnl_wei > 0) AS wins,
       count(DISTINCT r.token) AS tokens,
       sum(r.pnl_wei) FILTER (WHERE r.bt >= :w4 AND r.bt < :w3) AS wk4,
       sum(r.pnl_wei) FILTER (WHERE r.bt >= :w3 AND r.bt < :w2) AS wk3,
       sum(r.pnl_wei) FILTER (WHERE r.bt >= :w2 AND r.bt < :w1) AS wk2,
       sum(r.pnl_wei) FILTER (WHERE r.bt >= :w1) AS wk1
  FROM r
 WHERE NOT EXISTS (SELECT 1 FROM snipe s WHERE s.wallet = r.wallet AND s.token = r.token)
 GROUP BY r.wallet
"""


def tier_of(rank: int | None) -> tuple[str, str, str] | None:
    """(key, badge, title) for a rank, None when unranked or below the last tier."""
    if not rank:
        return None
    for last, key, badge, title in TIERS:
        if rank <= last:
            return key, badge, title
    return None


def _sign(v: Any) -> int | None:
    """+1 green week, -1 red week, 0 flat, None: no closed trade that week."""
    if v is None:
        return None
    v = int(v)
    return 1 if v > 0 else -1 if v < 0 else 0


@dataclass(frozen=True)
class Rules:
    window_days: float = 30.0
    min_closes: int = 5
    min_tokens: int = 2
    snipe_s: float = 60.0


DEFAULT_RULES = Rules()


def build_board(rows: Any, rules: Rules = DEFAULT_RULES) -> dict[str, dict]:
    """rows: (wallet, pnl_wei, pnl_7d_wei, closes, wins, tokens, wk4, wk3, wk2, wk1).
    -> wallet -> card payload. Ranked wallets get rank/of/tier. Pure."""
    out: dict[str, dict] = {}
    for w, pnl, p7, closes, wins, tokens, *weeks in rows:
        out[w] = {"pnl": int(pnl) / WEI, "pnl_7d": int(p7 or 0) / WEI, "closes": int(closes),
                  "wins": int(wins), "tokens": int(tokens), "weeks": [_sign(x) for x in weeks],
                  "days": int(rules.window_days)}
    ranked = sorted((w for w, v in out.items()
                     if v["closes"] >= rules.min_closes and v["tokens"] >= rules.min_tokens
                     and v["pnl"] > 0), key=lambda w: (-out[w]["pnl"], w))
    for i, w in enumerate(ranked, 1):
        out[w]["rank"], out[w]["of"] = i, len(ranked)
        t = tier_of(i)
        if t:
            out[w]["tier"] = t[0]
    return out


def load_board(conn: Any, *, chain_id: int, rules: Rules = DEFAULT_RULES,
               skip_wallets: Any = (), skip_tokens: Any = (),
               now: datetime | None = None) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    wk = timedelta(days=7)
    rows = conn.execute(sa.text(PERF_SQL), {
        "c": chain_id, "since": now - timedelta(days=rules.window_days),
        "snipe_s": float(rules.snipe_s), "skip_wallets": sorted(skip_wallets),
        "skip_tokens": sorted(skip_tokens),
        "w1": now - wk, "w2": now - 2 * wk, "w3": now - 3 * wk, "w4": now - 4 * wk}).all()
    return build_board([tuple(r) for r in rows], rules)
