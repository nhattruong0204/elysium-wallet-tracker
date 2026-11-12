"""Coordinated launch cohorts: groups of tracked wallets that keep entering new tokens together.

Pattern (2026-11-12, 16 Agora-leaderboard wallets): a new token every ~29 minutes; on each one
9-16 of the same wallets are among the first tracked buyers within seconds of each other; they
then sell into whoever follows. One such entry proves nothing (popular launches attract many
snipers); the same wallets doing it together on launch after launch does.

Rule, on our own `trades` rows (the website's read-only `public_api.trades` view has the same
columns, so the tracker, the website and the Consensus sender all judge identically):

1. Launch burst of a token: the wallets whose first trade of it lies within `window_s` of the
   first tracked trade of it, if there are at least `min_burst` of them.
2. Two wallets are linked when they were in the same burst on at least `min_shared` tokens,
   that is at least `min_overlap` of the bursts of the less active of the two, and at least
   `min_overlap_busy` of the bursts of the more active one (so a sniper that enters every
   launch on the chain is not pulled into whichever group it happens to share launches with).
3. A cohort is a connected group of at least `min_size` linked wallets. Its tokens are the
   bursts it was part of with at least `min_members` of its wallets.

No database writes. Thresholds live in IntelConfig.cohort_*.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any

import sqlalchemy as sa

BURST_SQL = """
WITH first AS (
    SELECT token, wallet, min(block_time) AS f
      FROM {table}
     WHERE chain_id = :c AND block_time >= :since
       AND NOT (token = ANY(CAST(:skip AS text[])))
     GROUP BY token, wallet),
t0 AS (SELECT token, min(f) AS m FROM first GROUP BY token),
burst AS (
    SELECT f.token, f.wallet
      FROM first f JOIN t0 ON t0.token = f.token
     WHERE f.f <= t0.m + make_interval(secs => :w))
SELECT b.token, b.wallet
  FROM burst b
 WHERE b.token IN (SELECT token FROM burst GROUP BY token HAVING count(*) >= :k)
"""


@dataclass(frozen=True)
class Cohort:
    wallets: frozenset[str]
    tokens: frozenset[str]

    @property
    def key(self) -> str:
        """Stable label for logs and the API: the lowest member address."""
        return min(self.wallets)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.key, "size": len(self.wallets), "wallets": sorted(self.wallets),
                "tokens": sorted(self.tokens), "launches": len(self.tokens)}


def find_cohorts(rows: Iterable[tuple[str, str]], *, min_shared: int = 5,
                 min_overlap: float = 0.6, min_size: int = 5,
                 min_members: int = 3, min_overlap_busy: float = 0.3) -> list[Cohort]:
    """rows: (token, wallet) launch-burst memberships. Pure function; largest cohort first."""
    by_token: dict[str, set[str]] = defaultdict(set)
    by_wallet: dict[str, set[str]] = defaultdict(set)
    for token, wallet in rows:
        by_token[token].add(wallet)
        by_wallet[wallet].add(token)
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for ws in by_token.values():
        for a, b in combinations(sorted(ws), 2):
            shared[(a, b)] += 1
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    linked: set[str] = set()
    for (a, b), n in shared.items():
        la, lb = len(by_wallet[a]), len(by_wallet[b])
        if (n >= min_shared and n >= min_overlap * min(la, lb)
                and n >= min_overlap_busy * max(la, lb)):
            linked.update((a, b))
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, set[str]] = defaultdict(set)
    for w in linked:
        groups[root(w)].add(w)
    out = []
    for ws in groups.values():
        if len(ws) < min_size:
            continue
        toks = frozenset(t for t, members in by_token.items() if len(members & ws) >= min_members)
        out.append(Cohort(frozenset(ws), toks))
    out.sort(key=lambda c: (-len(c.wallets), c.key))
    return out


def load_cohorts(conn: Any, *, table: str, chain_id: int, ic: Any,
                 skip_tokens: Iterable[str] = (), now: datetime | None = None) -> list[Cohort]:
    """Run the burst query on `table` ('trades' for the tracker, 'public_api.trades' for the
    read-only website) and group the result. `ic` is an IntelConfig."""
    if table not in ("trades", "public.trades", "public_api.trades"):
        raise ValueError(f"unexpected table {table!r}")
    since = (now or datetime.now(timezone.utc)) - timedelta(days=ic.cohort_lookback_days)
    rows = conn.execute(sa.text(BURST_SQL.format(table=table)), {
        "c": chain_id, "since": since, "w": float(ic.cohort_window_s),
        "k": int(ic.cohort_min_burst), "skip": sorted(set(skip_tokens))}).all()
    return find_cohorts(((r.token, r.wallet) for r in rows), min_shared=ic.cohort_min_shared,
                        min_overlap=ic.cohort_min_overlap, min_size=ic.cohort_min_size,
                        min_members=ic.cohort_min_members,
                        min_overlap_busy=ic.cohort_min_overlap_busy)


def members_of(cohorts: Iterable[Cohort]) -> dict[str, str]:
    """wallet -> cohort key."""
    return {w: c.key for c in cohorts for w in c.wallets}


def warning_line(n: int) -> str:
    """The one-line card warning (plain text; callers escape/format as needed)."""
    return (f"⚠️ Launch cohort: {n} linked wallet{'s' if n != 1 else ''} that enter launches "
            "together traded this token. Likely exit liquidity; do not follow blindly.")
