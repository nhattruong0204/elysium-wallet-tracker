"""Serial token launchers: tracked wallets that create tokens and sell them to whoever follows.

Pattern (review of 2026-11-19..26): 41 Agora-leaderboard wallets created 2,573 tokens in seven
days through the eleos launch contract, each time buying 50-100 HYPE in the creation transaction
itself and selling within ~1 minute to the snipers and bots that buy every new launch. That made
97% of the measured profit of all tracked wallets (+118k HYPE) and 68% of the Telegram trade
alerts, and it put them on Agora's top ranks. Following them means buying their exit.

Two ways to see it, giving the same answer (41 of 41 wallets, no false positive, on that week):

* real time (tracker): the receipt of the wallet's buy shows the token being minted in that same
  transaction (`creator_legs` in ScanResult). Proof, but only for new transactions.
* stored rows (tracker, website and Consensus sender): the wallet's first buy of a token lies
  within `window_s` of the launch time Agora reports for it (token_profiles.launched_at) and is
  the first tracked trade of the token. A wallet doing that on `min_launches` tokens within the
  lookback is a launcher. (A sniper landing in the creation second would also qualify; it is
  just as impossible to follow.)

Unqualified table names: the tracker runs this on `public`, the website and the sender on
`public_api` (their search_path), so all three judge identically. No database writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

ZERO_TOPIC = "0x" + "0" * 64

LAUNCH_SQL = """
WITH fb AS (
    SELECT wallet, token, min(block_time) AS fb
      FROM trades
     WHERE chain_id = :c AND kind = 'BUY' AND block_time >= :since
     GROUP BY wallet, token),
t0 AS (
    SELECT token, min(block_time) AS t0 FROM trades
     WHERE chain_id = :c AND token IN (SELECT token FROM fb) GROUP BY token)
SELECT fb.wallet, fb.token
  FROM fb
  JOIN t0 ON t0.token = fb.token
  JOIN token_profiles p ON p.chain_id = :c AND p.address = fb.token
 WHERE p.launched_at IS NOT NULL
   AND fb.fb BETWEEN p.launched_at - make_interval(secs => :w)
                 AND p.launched_at + make_interval(secs => :w)
   AND fb.fb <= t0.t0 + make_interval(secs => :w)
"""


@dataclass
class Launchers:
    """wallet -> number of tokens it launched in the lookback; tokens launched by a wallet that
    qualifies as a launcher."""
    wallets: dict[str, int] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)

    def is_launcher(self, wallet: str, min_launches: int) -> bool:
        return self.wallets.get(wallet, 0) >= min_launches


def find_launchers(rows: Any, *, min_launches: int) -> Launchers:
    """rows: (wallet, token) launch-second buys. Pure."""
    per: dict[str, set[str]] = {}
    for wallet, token in rows:
        per.setdefault(wallet, set()).add(token)
    out = Launchers()
    for wallet, toks in per.items():
        out.wallets[wallet] = len(toks)
        if len(toks) >= min_launches:
            out.tokens |= toks
    return out


def load_launchers(conn: Any, *, chain_id: int, lookback_days: float, window_s: float,
                   min_launches: int, now: datetime | None = None) -> Launchers:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
    rows = conn.execute(sa.text(LAUNCH_SQL), {"c": chain_id, "since": since,
                                              "w": float(window_s)}).all()
    return find_launchers(((r[0], r[1]) for r in rows), min_launches=min_launches)


def minted_tokens(receipt: dict, transfer_topic: str) -> set[str]:
    """Tokens minted (Transfer from the zero address) inside one transaction receipt."""
    out: set[str] = set()
    for lg in receipt.get("logs") or ():
        topics = list(lg.get("topics") or [])
        if len(topics) >= 2 and topics[0] == transfer_topic and topics[1] == ZERO_TOPIC:
            out.add(str(lg.get("address", "")).lower())
    return out


def warning_line() -> str:
    return ("⚠️ Launched by a serial token launcher (creates tokens and sells them to the first "
            "buyers within minutes). Likely exit liquidity.")
