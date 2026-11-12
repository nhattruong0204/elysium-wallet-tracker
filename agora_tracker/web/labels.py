"""Wallet labels: what kind of wallet each tracked address is, not only how it performed.

Read model only, rule-based and explainable. Every label is computed from our own stored rows
(the website's read-only `public_api` views), over a fixed lookback (`LabelRules.lookback_days`,
independent of the table's window, so a wallet's labels do not change when you switch the
window), and carries the evidence that produced it (`label_reasons`).

Three families, all on one record:

* status   one overall verdict, first match wins (most severe first):
           blocked    on the operator block list (TRACKING_BLOCKED_WALLETS)
           contract   not followed: the address is a contract (pool, router, vault)
           cohort     member of a coordinated launch cohort (agora_tracker/cohorts.py)
           bundler    keeps buying the same token in the same block as the same other tracked
                      wallet (5+ blocks on 3+ tokens with one partner)
           farm       a large share of its legs are token transfers to/from other tracked
                      wallets in the same transaction (moving bags around, not trading)
           automated  machine-like transaction timing with almost no decoded buys/sells
                      (the tracker's automated-wallet rule)
           dormant    no action of its own in the lookback (receipts do not count)
           organic    none of the above
  `flags` lists every one of blocked/contract/cohort/bundler/farm/automated that applies (a
  cohort wallet is often a bundler too); `status` is only the most severe.
* style    one primary trading style from the lookback's legs: sniper | scalper | swing |
           holder | mover | mixed | thin (too few decoded buys/sells to say). `style_tags` adds the secondary
           traits: high_frequency, early_buyer, concentrated, diversified, linked.
* tiers    size (median verified buy: micro | small | mid | large | whale), activity
           (active | cooling | dormant, from its last own leg; receipts do not count) and
           performance_tier (measured closes in the lookback only, never Agora's PnL:
           consistent_winner | emerging | mixed | persistent_loser | insufficient_history).
* evidence every label has its reason text; `label_confidence` (confirmed | high | medium | low |
           insufficient) is a documented heuristic from evidence count and data quality, not a
           probability; `relationship` separates "none_detected" (enough trading seen, nothing
           found) from "unknown" (too little to say).

Not derivable from stored rows today, so deliberately absent rather than guessed: momentum
(no reliable entry-time market history), diamond hands (our history is shorter than any honest
holding horizon), shared funders (native-coin funding transfers are not indexed), insider and
wash-trading labels (no independent evidence to anchor them).

Caveats, shown on the page: "launch" means the first trade of the token *we* recorded (tokens
first seen in the first day of our history are not counted as observed launches); hold time is
first buy to first later sell of the token; transfers and same-block buys are evidence of
coordination, not proof of common control.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations
from typing import Any

UTC = timezone.utc
E18 = Decimal(10) ** 18
VERSION = "labels-v1"

STATUSES = ("blocked", "contract", "cohort", "bundler", "farm", "automated", "dormant",
            "unknown", "organic")
FLAGS = (*STATUSES[:6], "muted")      # muted is a flag only: the status says what the wallet is
STYLES = ("sniper", "scalper", "swing", "holder", "mover", "mixed", "thin")
STYLE_TAGS = ("high_frequency", "high_churn", "accumulator", "early_buyer", "concentrated",
              "diversified", "linked")
SIZES = ("micro", "small", "mid", "large", "whale")
ACTIVITY = ("active", "cooling", "dormant")
TIERS = ("consistent_winner", "emerging", "mixed", "persistent_loser", "insufficient_history")
CONFIDENCES = ("confirmed", "high", "medium", "low", "insufficient")
RELATIONSHIPS = ("launch_cohort", "bundle_pair", "transfer_linked", "none_detected", "unknown")

STATUS_NOTES = {
    "blocked": "on the operator block list: not followed, no alerts",
    "contract": "a contract address (pool, router, vault), not followed",
    "cohort": "enters new launches together with the same group of tracked wallets",
    "bundler": "keeps buying the same token in the same block as the same other tracked wallet",
    "farm": "a large share of its legs are transfers with other tracked wallets",
    "automated": "machine-like timing, almost no decoded buys/sells",
    "dormant": "no action of its own in the lookback",
    "unknown": "active, but too few decoded buys/sells to say anything about it",
    "organic": "enough observed trading and no coordination evidence found",
}
STYLE_NOTES = {
    "sniper": "first buy within a minute of most launches it bought",
    "scalper": "median hold under 15 min",
    "swing": "median hold 15 min to 3 days",
    "holder": "rarely sells, or holds over 3 days",
    "mover": "mostly token transfers, few decoded trades",
    "mixed": "no dominant pattern",
    "thin": "too few decoded buys/sells to say",
}


@dataclass(frozen=True)
class LabelRules:
    """Thresholds. Changing any of them changes what the labels mean: bump VERSION."""
    lookback_days: int = 30
    launch_window_s: int = 60           # first buy this close to the token's first trade = early
    history_grace_h: int = 24           # tokens first seen this soon after history began are not
    #                                     observed launches (they existed before we looked)
    min_style_legs: int = 8             # BUY+SELL legs needed to name a style
    sniper_min_launches: int = 5        # observed launches bought
    sniper_share: float = 0.5           # ...of which bought early
    early_share: float = 0.25           # secondary tag
    scalper_hold_s: int = 15 * 60       # median first-buy -> first-sell
    swing_hold_s: int = 3 * 86400       # above: holder
    min_round_trips: int = 4
    holder_sell_ratio: float = 0.25     # sells per buy at or below: holder
    mover_min_transfers: int = 20       # few decoded trades but this many transfers: mover
    mover_transfer_share: float = 0.7   # ...that are at least this share of the legs
    hf_legs_per_day: float = 40.0       # legs per active day
    concentrated_share: float = 0.5     # top token's share of verified buy HYPE
    min_concentrated_buys: int = 5
    diversified_tokens: int = 30
    linked_min: int = 3                 # same-tx transfers with other tracked wallets
    farm_min: int = 5                   # farm: at least this many such transfers,
    farm_min_peers: int = 2             # ...with at least this many different tracked wallets,
    farm_share: float = 0.1             # ...and at least this share of all its legs
    bundle_min_blocks: int = 5          # bundler: bought the same token in the same block as
    bundle_min_tokens: int = 3          # ONE other tracked wallet this often, on this many tokens
    bundle_max_block_wallets: int = 50  # blocks with more tracked buyers than this are ignored
    size_edges_hype: tuple[float, ...] = (1.0, 5.0, 25.0, 100.0)    # micro|small|mid|large|whale
    active_days: int = 7
    cooling_days: int = 30
    churn_min_round_trips: int = 20     # high_churn: this many round trips,
    churn_hold_s: int = 5 * 60          # ...median hold at most this,
    churn_balance: float = 0.8          # ...and sells/buys within [x, 1/x] (flat net position)
    accumulator_buys_per_token: float = 3.0
    accumulator_min_tokens: int = 3
    accumulator_sell_ratio: float = 0.5
    proven_min_closes: int = 10         # consistent_winner / persistent_loser need this many
    proven_win_rate: float = 0.5
    proven_profit_factor: float = 1.5   # gross measured wins / gross measured losses
    loser_win_rate: float = 0.4
    edge_min_closes: int = 3            # fewer measured closes: insufficient_history
    high_conf_trade_legs: int = 40      # style confidence: high from this many decoded legs
    cohort_high_launches: int = 10      # flag confidence thresholds (heuristic, not a probability)
    bundle_high_blocks: int = 10
    farm_medium_transfers: int = 10
    # automated: the tracker's rule (IntelConfig.bot_*), copied in by `from_intel`
    bot_lookback_days: int = 7
    bot_min_txs: int = 100
    bot_fast_gap_s: float = 2.0
    bot_fast_share: float = 0.15
    bot_max_trade_share: float = 0.05

    @classmethod
    def from_intel(cls, ic: Any, **kw: Any) -> LabelRules:
        return cls(bot_lookback_days=ic.bot_lookback_days, bot_min_txs=ic.bot_min_txs,
                   bot_fast_gap_s=ic.bot_fast_gap_s, bot_fast_share=ic.bot_fast_share,
                   bot_max_trade_share=ic.bot_max_trade_share, **kw)

    def describe(self) -> dict[str, Any]:
        return {"version": VERSION, **asdict(self)}


# One pass over the lookback's legs of public wallets. Unqualified names: the website's
# search_path is public_api, so this runs on the privacy-filtered views.
FEATURES_SQL = """
WITH t AS (
  SELECT wallet, token, kind, confidence, hype_wei, block_number, block_time, tx_hash
    FROM trades
   WHERE chain_id = :c AND block_time >= :since
     AND NOT (token = ANY(CAST(:skip AS text[])))),
hist AS (SELECT min(block_time) AS h FROM trades WHERE chain_id = :c),
t0 AS (
  SELECT token, min(block_time) AS t0 FROM trades
   WHERE chain_id = :c AND token IN (SELECT DISTINCT token FROM t) GROUP BY token),
leg AS (
  SELECT wallet, count(*) AS legs,
    count(*) FILTER (WHERE kind = 'BUY') AS buys,
    count(*) FILTER (WHERE kind = 'SELL') AS sells,
    count(*) FILTER (WHERE kind IN ('TRANSFER_IN', 'TRANSFER_OUT')) AS transfers,
    count(DISTINCT token) AS tokens,
    count(DISTINCT token) FILTER (WHERE kind = 'BUY') AS bought_tokens,
    count(DISTINCT date_trunc('day', block_time)) AS active_days,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY hype_wei)
      FILTER (WHERE kind = 'BUY' AND confidence = 'high' AND hype_wei > 0) AS med_buy_wei,
    count(*) FILTER (WHERE kind = 'BUY' AND confidence = 'high' AND hype_wei > 0) AS verified_buys,
    coalesce(sum(hype_wei) FILTER (WHERE kind = 'BUY' AND confidence = 'high'), 0) AS buy_wei,
    max(block_time) FILTER (WHERE kind <> 'TRANSFER_IN') AS last_own_at
  FROM t GROUP BY wallet),
tok AS (
  SELECT wallet, token,
    min(block_time) FILTER (WHERE kind = 'BUY') AS first_buy,
    coalesce(sum(hype_wei) FILTER (WHERE kind = 'BUY' AND confidence = 'high'), 0) AS buy_wei
  FROM t GROUP BY wallet, token),
exit AS (
  SELECT k.wallet, k.token, min(s.block_time) AS first_sell
    FROM tok k JOIN t s ON s.wallet = k.wallet AND s.token = k.token AND s.kind = 'SELL'
                       AND s.block_time > k.first_buy
   GROUP BY k.wallet, k.token),
per AS (
  SELECT k.wallet,
    count(*) FILTER (WHERE k.first_buy IS NOT NULL
                     AND t0.t0 >= (SELECT h FROM hist) + make_interval(hours => :grace))
      AS launches,
    count(*) FILTER (WHERE k.first_buy IS NOT NULL
                     AND t0.t0 >= (SELECT h FROM hist) + make_interval(hours => :grace)
                     AND k.first_buy <= t0.t0 + make_interval(secs => :lw)) AS early,
    count(x.first_sell) AS round_trips,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM x.first_sell - k.first_buy))
      AS med_hold_s,
    max(k.buy_wei) AS top_buy_wei
  FROM tok k JOIN t0 ON t0.token = k.token
  LEFT JOIN exit x ON x.wallet = k.wallet AND x.token = k.token
  GROUP BY k.wallet),
xfer AS (
  SELECT o.wallet, count(*) AS internal, count(DISTINCT i.wallet) AS peers
    FROM t o JOIN t i ON i.tx_hash = o.tx_hash AND i.token = o.token AND i.wallet <> o.wallet
     AND ((o.kind = 'TRANSFER_OUT' AND i.kind = 'TRANSFER_IN')
       OR (o.kind = 'TRANSFER_IN' AND i.kind = 'TRANSFER_OUT'))
   GROUP BY o.wallet),
tx AS (
  SELECT wallet, tx_hash, min(block_time) AS bt, bool_or(kind IN ('BUY', 'SELL')) AS traded
    FROM t WHERE block_time >= :since_bot GROUP BY wallet, tx_hash),
gap AS (
  SELECT wallet, traded,
         extract(epoch FROM bt - lag(bt) OVER (PARTITION BY wallet ORDER BY bt, tx_hash)) AS s
    FROM tx),
bot AS (
  SELECT wallet, count(*) AS txs,
         avg(CASE WHEN s < :gap THEN 1.0 ELSE 0.0 END) FILTER (WHERE s IS NOT NULL) AS fast
    FROM gap GROUP BY wallet),
botleg AS (
  SELECT wallet, avg(CASE WHEN kind IN ('BUY', 'SELL') THEN 1.0 ELSE 0.0 END) AS trade_share
    FROM t WHERE block_time >= :since_bot GROUP BY wallet),
rp AS (
  SELECT r.wallet, count(*) FILTER (WHERE r.status = 'measured') AS closes,
    count(*) FILTER (WHERE r.status = 'measured' AND r.pnl_wei > 0) AS wins,
    coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'measured'), 0) AS pnl_wei,
    coalesce(sum(r.pnl_wei) FILTER (WHERE r.status = 'measured' AND r.pnl_wei > 0), 0) AS win_wei,
    coalesce(-sum(r.pnl_wei) FILTER (WHERE r.status = 'measured' AND r.pnl_wei < 0), 0) AS loss_wei
  FROM realized_pnl r JOIN trades tt ON tt.id = r.trade_id
  WHERE r.chain_id = :c AND tt.block_time >= :since GROUP BY r.wallet)
SELECT l.wallet, l.legs, l.buys, l.sells, l.transfers, l.tokens, l.bought_tokens,
       l.active_days, l.med_buy_wei, l.verified_buys, l.buy_wei, l.last_own_at,
       coalesce(p.launches, 0) AS launches, coalesce(p.early, 0) AS early,
       coalesce(p.round_trips, 0) AS round_trips, p.med_hold_s, p.top_buy_wei,
       coalesce(x.internal, 0) AS internal, coalesce(x.peers, 0) AS peers,
       coalesce(bo.txs, 0) AS bot_txs, bo.fast AS bot_fast, bl.trade_share AS bot_trade_share,
       coalesce(rp.closes, 0) AS closes, coalesce(rp.wins, 0) AS wins,
       coalesce(rp.pnl_wei, 0) AS pnl_wei, coalesce(rp.win_wei, 0) AS win_wei,
       coalesce(rp.loss_wei, 0) AS loss_wei
  FROM leg l
  LEFT JOIN per p ON p.wallet = l.wallet
  LEFT JOIN xfer x ON x.wallet = l.wallet
  LEFT JOIN bot bo ON bo.wallet = l.wallet
  LEFT JOIN botleg bl ON bl.wallet = l.wallet
  LEFT JOIN rp ON rp.wallet = l.wallet
"""


# Same-block buys: every BUY leg in a block where another tracked wallet bought the same token.
BLOCKS_SQL = """
SELECT b.wallet, b.token, b.block_number
  FROM trades b
  JOIN (SELECT token, block_number FROM trades
         WHERE chain_id = :c AND kind = 'BUY' AND block_time >= :since
           AND NOT (token = ANY(CAST(:skip AS text[])))
         GROUP BY token, block_number HAVING count(DISTINCT wallet) >= 2) g
    ON g.token = b.token AND g.block_number = b.block_number
 WHERE b.chain_id = :c AND b.kind = 'BUY' AND b.block_time >= :since
 GROUP BY b.wallet, b.token, b.block_number
"""


def bundle_partners(rows: Iterable[tuple[str, str, int]], *, min_blocks: int,
                    min_tokens: int, max_block_wallets: int = 50) -> dict[str, dict[str, Any]]:
    """rows: (wallet, token, block) same-block buys. A wallet is a bundler when ONE other
    tracked wallet bought the same token in the same block as it at least `min_blocks` times,
    on at least `min_tokens` different tokens. Sharing a busy block with whoever else sniped
    it is common; doing it again and again with the same partner is not. Pure function.
    Pairs are reported as pairs: A-B and B-C never make A-C related (no transitive merging).
    Returns {wallet: {"partners", "blocks", "tokens", "with"}} (blocks/tokens: with the best
    partner; with: the partner addresses)."""
    by_block: dict[tuple[str, int], set[str]] = {}
    for w, tok, blk in rows:
        by_block.setdefault((tok, int(blk)), set()).add(w)
    pair_blocks: dict[tuple[str, str], int] = {}
    pair_tokens: dict[tuple[str, str], set[str]] = {}
    for (tok, _), ws in by_block.items():
        if len(ws) > max_block_wallets:     # everybody landed in it: says nothing about pairs
            continue
        for a, b in combinations(sorted(ws), 2):
            pair_blocks[(a, b)] = pair_blocks.get((a, b), 0) + 1
            pair_tokens.setdefault((a, b), set()).add(tok)
    out: dict[str, dict[str, Any]] = {}
    for (a, b), nb in pair_blocks.items():
        nt = len(pair_tokens[(a, b)])
        if nb < min_blocks or nt < min_tokens:
            continue
        for w in (a, b):
            cur = out.setdefault(w, {"partners": 0, "blocks": 0, "tokens": 0, "with": []})
            cur["partners"] += 1
            cur["with"].append(b if w == a else a)
            if (nb, nt) > (cur["blocks"], cur["tokens"]):
                cur["blocks"], cur["tokens"] = nb, nt
    return out


def feature_params(rules: LabelRules, chain_id: int, now: datetime,
                   skip_tokens: Iterable[str] = ()) -> dict[str, Any]:
    return {"c": chain_id, "since": now - timedelta(days=rules.lookback_days),
            "since_bot": now - timedelta(days=rules.bot_lookback_days),
            "skip": sorted(set(skip_tokens)), "grace": int(rules.history_grace_h),
            "lw": float(rules.launch_window_s), "gap": float(rules.bot_fast_gap_s)}


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _share(a: float, b: float) -> float | None:
    return (a / b) if b else None


def classify(wallet: str, f: Mapping[str, Any] | None, rules: LabelRules, *,
             enrollment: str = "active", now: datetime,
             blocked: frozenset[str] | set[str] = frozenset(),
             cohort: Mapping[str, Any] | None = None,
             bundle: Mapping[str, Any] | None = None,
             quality: str | None = None,
             muted: frozenset[str] | set[str] = frozenset()) -> dict[str, Any]:
    """One wallet's labels. `f`: its FEATURES_SQL row (None: no legs in the lookback).
    `cohort`: {"id", "size", "launches"} when the wallet is a cohort member; `bundle`: its
    `bundle_partners` entry; `quality`: the wallet's data-quality class (intel.classify_quality),
    which caps the confidence when our history of the wallet has holes. Pure.

    Confidence is a documented heuristic from evidence count and data quality (confirmed: an
    operator or chain fact; high / medium / low; insufficient), NOT a calibrated probability."""
    f = dict(f or {})
    conf: dict[str, str] = {}
    n = lambda k: int(f.get(k) or 0)  # noqa: E731
    legs, buys, sells, transfers = n("legs"), n("buys"), n("sells"), n("transfers")
    trade_legs = buys + sells
    reasons: list[str] = []
    flags: list[str] = []

    # --- flags (coordination / non-trader evidence) --------------------------------------------
    if wallet in blocked:
        flags.append("blocked")
        conf["blocked"] = "confirmed"
        reasons.append("blocked: on the operator block list")
    if enrollment == "excluded" and wallet not in blocked:
        flags.append("contract")
        conf["contract"] = "confirmed"
        reasons.append("contract: a contract address, not followed")
    if cohort:
        flags.append("cohort")
        conf["cohort"] = "high" if cohort["launches"] >= rules.cohort_high_launches else "medium"
        reasons.append(f"cohort: one of {cohort['size']} wallets entering {cohort['launches']} "
                       f"launches together (cohort {cohort['id'][:10]})")
    bought = n("bought_tokens")
    bundle = bundle or {}
    bblocks, btoks, partners = (int(bundle.get(k, 0)) for k in ("blocks", "tokens", "partners"))
    if partners:
        flags.append("bundler")
        conf["bundler"] = "high" if bblocks >= rules.bundle_high_blocks else "medium"
        reasons.append(f"bundler: {bblocks} same-block buys on {btoks} tokens with the same "
                       f"tracked wallet ({partners} such partner{'s' if partners != 1 else ''})")
    internal, peers = n("internal"), n("peers")
    if (internal >= rules.farm_min and peers >= rules.farm_min_peers and legs
            and internal / legs >= rules.farm_share):
        flags.append("farm")
        conf["farm"] = "medium" if internal >= rules.farm_medium_transfers else "low"
        reasons.append(f"farm: {internal} of {legs} legs are transfers with {peers} other "
                       "tracked wallet(s) in the same transaction")
    fast, tshare = _f(f.get("bot_fast")), _f(f.get("bot_trade_share"))
    if (n("bot_txs") >= rules.bot_min_txs and fast is not None and fast >= rules.bot_fast_share
            and tshare is not None and tshare <= rules.bot_max_trade_share):
        flags.append("automated")
        conf["automated"] = "high" if n("bot_txs") >= 3 * rules.bot_min_txs else "medium"
        reasons.append(f"automated: {n('bot_txs')} txs in {rules.bot_lookback_days}d, "
                       f"{fast:.0%} under {rules.bot_fast_gap_s:g}s apart, {tshare:.0%} buys/sells")

    # --- activity ------------------------------------------------------------------------------
    # own actions only: a token sent to the wallet (airdrop spam) is not activity
    last_own = f.get("last_own_at")
    age_d = (now - last_own).total_seconds() / 86400 if last_own else None
    activity = ("dormant" if age_d is None or age_d > rules.cooling_days else
                "cooling" if age_d > rules.active_days else "active")
    if wallet in muted and wallet not in blocked:
        flags.append("muted")
        conf["muted"] = "confirmed"
        reasons.append("muted: on the operator mute list (tracked and shown here, never alerted)")
    status = next((s for s in STATUSES[:6] if s in flags), None)
    if status is None:
        # no evidence of coordination is only a finding when there was enough trading to see
        status = ("dormant" if last_own is None else
                  "unknown" if trade_legs < rules.min_style_legs else "organic")
    relationship = ("launch_cohort" if cohort else "bundle_pair" if partners else
                    "transfer_linked" if internal >= rules.linked_min else
                    "unknown" if trade_legs < rules.min_style_legs else "none_detected")

    # --- style ---------------------------------------------------------------------------------
    launches, early = n("launches"), n("early")
    rt, hold = n("round_trips"), _f(f.get("med_hold_s"))
    tags: list[str] = []
    if trade_legs < rules.min_style_legs:
        if transfers >= rules.mover_min_transfers and transfers / legs >= rules.mover_transfer_share:
            style = "mover"
            reasons.append(f"mover: {transfers} of {legs} legs are token transfers, not decoded "
                           "buys/sells (moves tokens, or trades on a venue we do not decode)")
        else:
            style = "thin"
    elif launches >= rules.sniper_min_launches and early / launches >= rules.sniper_share:
        style = "sniper"
        reasons.append(f"sniper: first buy within {rules.launch_window_s}s of launch on {early} "
                       f"of {launches} observed launches")
    elif rt >= rules.min_round_trips and hold is not None and hold <= rules.scalper_hold_s:
        style = "scalper"
        reasons.append(f"scalper: median hold {hold / 60:.0f} min over {rt} round trips")
    elif buys and (sells / buys <= rules.holder_sell_ratio
                   or (rt >= rules.min_round_trips and hold is not None
                       and hold > rules.swing_hold_s)):
        style = "holder"
        reasons.append(f"holder: {sells} sells per {buys} buys" + (
            f", median hold {hold / 3600:.0f} h" if hold is not None else ""))
    elif rt >= rules.min_round_trips and hold is not None:
        style = "swing"
        reasons.append(f"swing: median hold {hold / 3600:.1f} h over {rt} round trips")
    else:
        style = "mixed"
    days = n("active_days")
    if days and legs / days >= rules.hf_legs_per_day:
        tags.append("high_frequency")
    if (rt >= rules.churn_min_round_trips and hold is not None and hold <= rules.churn_hold_s
            and buys and rules.churn_balance <= sells / buys <= 1 / rules.churn_balance):
        tags.append("high_churn")
        reasons.append(f"high_churn: {rt} round trips, median hold {hold / 60:.0f} min, "
                       f"{sells} sells per {buys} buys (flat net position; consistent with "
                       "volume farming, not proof of it)")
    if (bought >= rules.accumulator_min_tokens and buys / bought >= rules.accumulator_buys_per_token
            and sells / buys <= rules.accumulator_sell_ratio):
        tags.append("accumulator")
    if style != "sniper" and launches >= rules.sniper_min_launches and \
            early / launches >= rules.early_share:
        tags.append("early_buyer")
    buy_wei, top = Decimal(f.get("buy_wei") or 0), Decimal(f.get("top_buy_wei") or 0)
    if n("verified_buys") >= rules.min_concentrated_buys and buy_wei > 0 \
            and top / buy_wei >= Decimal(str(rules.concentrated_share)):
        tags.append("concentrated")
    if bought >= rules.diversified_tokens:
        tags.append("diversified")
    if internal >= rules.linked_min:
        tags.append("linked")

    # --- size and edge -------------------------------------------------------------------------
    med = f.get("med_buy_wei")
    med_buy = (Decimal(str(med)) / E18) if med is not None else None
    size = None
    if med_buy is not None:
        size = SIZES[sum(1 for e in rules.size_edges_hype if med_buy >= Decimal(str(e)))]
    closes, wins = n("closes"), n("wins")
    pnl = Decimal(f.get("pnl_wei") or 0) / E18
    win, loss = Decimal(f.get("win_wei") or 0), Decimal(f.get("loss_wei") or 0)
    pf = (win / loss) if loss > 0 else None           # None: no measured loss (or no closes)
    wr = (wins / closes) if closes else 0.0
    # measured closes only (sells whose cost basis we know); Agora's PnL is never used here
    if closes < rules.edge_min_closes:
        tier = "insufficient_history"
    elif (closes >= rules.proven_min_closes and pnl > 0 and wr >= rules.proven_win_rate
          and (pf is None or pf >= Decimal(str(rules.proven_profit_factor)))):
        tier = "consistent_winner"
    elif closes >= rules.proven_min_closes and pnl < 0 and wr < rules.loser_win_rate:
        tier = "persistent_loser"
    elif pnl > 0 and wr >= rules.proven_win_rate and closes < rules.proven_min_closes:
        tier = "emerging"
    else:
        tier = "mixed"

    # --- confidence ------------------------------------------------------------------------------
    style_conf = ("insufficient" if style == "thin" else
                  "high" if trade_legs >= rules.high_conf_trade_legs else "medium")
    if style == "mover":
        style_conf = "low"                 # undecoded activity: we see that it moves, not why
    overall = conf.get(status) or ("insufficient" if status in ("unknown", "dormant")
                                   else style_conf)
    if quality in ("gaps", "unscanned") and overall in ("high", "medium"):
        overall = "low"                    # holes in our history of the wallet
        reasons.append(f"confidence capped: data quality is '{quality}'")
    return {
        "label_status": status, "label_flags": flags, "style": style, "style_tags": tags,
        "size_tier": size, "activity": activity, "performance_tier": tier,
        "label_confidence": overall, "flag_confidence": conf, "style_confidence": style_conf,
        "relationship": relationship, "bundle_with": sorted(bundle.get("with", ())),
        "label_reasons": reasons,
        "cohort_id": cohort["id"] if cohort else None,
        "cohort_size": cohort["size"] if cohort else None,
        "label_metrics": {
            "lookback_legs": legs, "lookback_trade_legs": trade_legs,
            "transfer_share": _share(transfers, legs), "observed_launches": launches,
            "early_launches": early, "early_share": _share(early, launches),
            "round_trips": rt, "median_hold_min": (hold / 60) if hold is not None else None,
            "legs_per_active_day": _share(legs, days),
            "median_buy_hype": med_buy,
            "top_token_share": (top / buy_wei) if buy_wei > 0 else None,
            "internal_transfers": internal, "transfer_peers": peers,
            "bundle_blocks": bblocks, "bundle_tokens": btoks, "bundle_partners": partners,
            "bot_txs": n("bot_txs"), "bot_fast_share": fast, "bot_trade_share": tshare,
            "lookback_closes": closes, "lookback_wins": wins, "profit_factor": pf,
            "lookback_measured_pnl": pnl if closes else None},
    }


def compute_labels(conn: Any, *, chain_id: int, rules: LabelRules, now: datetime,
                   enrollment: Mapping[str, str], blocked: Iterable[str] = (),
                   quality: Mapping[str, str] | None = None, muted: Iterable[str] = (),
                   cohorts: Iterable[Any] = (), skip_tokens: Iterable[str] = ()
                   ) -> dict[str, dict[str, Any]]:
    """Labels for every wallet in `enrollment` ({wallet: enrollment status}). Two read-only
    queries on `conn` (the website's public_api connection); `cohorts`: the launch cohorts
    already computed by the same rule as the tracker."""
    import sqlalchemy as sa
    prm = feature_params(rules, chain_id, now, skip_tokens)
    feats = {r["wallet"]: dict(r) for r in conn.execute(sa.text(FEATURES_SQL), prm).mappings()}
    rows = conn.execute(sa.text(BLOCKS_SQL), {"c": chain_id, "since": prm["since"],
                                               "skip": prm["skip"]}).all()
    bundles = bundle_partners(((r[0], r[1], r[2]) for r in rows),
                              min_blocks=rules.bundle_min_blocks,
                              min_tokens=rules.bundle_min_tokens,
                              max_block_wallets=rules.bundle_max_block_wallets)
    co = {w: {"id": c.key, "size": len(c.wallets), "launches": len(c.tokens)}
          for c in cohorts for w in c.wallets}
    bl = frozenset(blocked)
    mu = frozenset(muted)
    return {w: classify(w, feats.get(w), rules, enrollment=st, now=now, blocked=bl,
                        cohort=co.get(w), bundle=bundles.get(w), quality=(quality or {}).get(w),
                        muted=mu)
            for w, st in enrollment.items()}


def unlabeled() -> dict[str, Any]:
    """Placeholder when labels could not be computed yet: every field present, all unknown."""
    return {"label_status": None, "label_flags": [], "style": None, "style_tags": [],
            "size_tier": None, "activity": None, "performance_tier": None,
            "label_confidence": None, "flag_confidence": {}, "style_confidence": None,
            "relationship": None, "bundle_with": [], "label_reasons": [],
            "cohort_id": None, "cohort_size": None, "label_metrics": {}}


def label_kpis(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Counts per status over the whole tracked set."""
    out = {s: 0 for s in STATUSES}
    for w in rows:
        if w.get("label_status") in out:
            out[w["label_status"]] += 1
    return out


def label_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The classification summary above the table. `status` counts are mutually exclusive;
    `flags`, `tags` and the cluster counts overlap (one wallet can be in several)."""
    rows = list(rows)
    done = [w for w in rows if w.get("label_status")]

    def tally(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for w in done:
            vals = w.get(key)
            for x in (vals if isinstance(vals, list) else [vals]):
                if x:
                    out[x] = out.get(x, 0) + 1
        return out
    return {
        "wallets": len(rows), "labelled": len(done),
        "insufficient": sum(1 for w in done if w["label_confidence"] == "insufficient"),
        "classified": sum(1 for w in done if w["label_confidence"] != "insufficient"),
        "cohorts": len({w["cohort_id"] for w in done if w.get("cohort_id")}),
        "in_cohorts": sum(1 for w in done if w.get("cohort_id")),
        "bundle_pair_wallets": sum(1 for w in done if "bundler" in w["label_flags"]),
        "consistent_winners_clean": sum(
            1 for w in done if w["performance_tier"] == "consistent_winner"
            and not w["label_flags"]),
        "styles": tally("style"), "flags": tally("label_flags"), "tags": tally("style_tags"),
        "tiers": tally("performance_tier"), "confidence": tally("label_confidence"),
        "relationships": tally("relationship")}
