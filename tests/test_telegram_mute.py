# ruff: noqa: F811
"""Hotfix: Telegram-only mute of Elysium mainnet USDC (alerts.telegram_mute_tokens).

The token stays indexed (trades, raw evidence, positions, PnL, cursor, signals, website); only
its Telegram alerts stop, including ones queued before the mute. A trade of another token that
merely used USDC as the quote asset still alerts. Matching is by exact contract address."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from test_cards import FakeResp
from test_tracker import E18, TOKEN, W1, W2, W3, build, chain, get_pos, run_all  # noqa: F401

from agora_tracker.chain import ScanResult
from agora_tracker.classify import TradeEvent
from agora_tracker.config import AlertConfig, ChainConfig, TelegramConfig, load_config
from agora_tracker.engine import Engine
from agora_tracker.notify import Notifier

# Bridged USDC on Elysium mainnet (chain 99901): the Orbit-gateway representation of Circle's
# HyperEVM USDC 0xb88339CB7199b77E23DB6E890353E22632Ba630f (see docs/OPERATIONS.md).
USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"
USDC_CHECKSUM = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"
FAKE_USDC = "0x" + "ee" * 20                  # a look-alike token with symbol "USDC"
TOKEN_X = "0x" + "c1" * 20


def test_the_verified_usdc_contract_is_what_production_mutes():
    import yaml
    compose = yaml.safe_load(open("docker-compose.yml"))
    env = compose["services"]["tracker"]["environment"]
    assert env["ALERTS_TELEGRAM_MUTE_TOKENS"] == USDC
    assert USDC == USDC_CHECKSUM.lower() and len(USDC) == 42
    # only the tracker carries it: ui/web keep their exact previous configuration
    for svc in ("ui", "web", "migrate"):
        e = compose["services"][svc].get("environment") or {}
        assert "ALERTS_TELEGRAM_MUTE_TOKENS" not in (e if isinstance(e, dict) else {})


def test_config_normalizes_case_merges_the_environment_and_rejects_garbage(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(json.dumps({"chain": {"rpc_urls": ["http://x"]},
                             "alerts": {"telegram_mute_tokens": [USDC_CHECKSUM]}}))
    monkeypatch.setenv("ALERTS_TELEGRAM_MUTE_TOKENS", f" {USDC_CHECKSUM.upper().replace('0X', '0x')},"
                                                      f"{TOKEN_X} ")
    cfg = load_config(p)
    assert cfg.alerts.telegram_mute_tokens == sorted({USDC, TOKEN_X})
    assert cfg.alerts.mute_tokens == [] and cfg.alerts.ignore_tokens == []   # untouched
    monkeypatch.setenv("ALERTS_TELEGRAM_MUTE_TOKENS", "USDC")                # never by symbol
    with pytest.raises(ValueError):
        load_config(p)


def engine_for(store, chain, muted):
    store, follower, engine = build(store, chain)
    engine._tg_muted = set(muted)
    return store, follower, engine


def outbox(store):
    with store.tx() as c:
        return [dict(r) for r in c.execute(sa.text(
            "SELECT id, dedup_key, status, last_error, payload FROM notification_outbox ORDER BY id"))
            .mappings()]


def state(store):
    with store.tx() as c:
        q = lambda sql: [tuple(r) for r in c.execute(sa.text(sql))]   # noqa: E731
        return {"trades": q("SELECT tx_hash, wallet, token, kind, qty FROM trades ORDER BY id"),
                "positions": q("SELECT wallet, token, qty, cost_wei, realized_wei, basis "
                               "FROM wallet_positions ORDER BY wallet, token"),
                "realized": q("SELECT wallet, token, pnl_wei, status FROM realized_pnl ORDER BY id"),
                "signals": q("SELECT kind, token FROM signals ORDER BY kind"),
                "evidence": q("SELECT count(*) FROM token_transfers"),
                "cursor": store.get_cursor()}


def test_muting_the_traded_token_stops_every_telegram_alert_and_changes_nothing_else(
        store, chain):
    """The fake chain's only token plays USDC: buys, sells, transfers and the cluster."""
    store, follower, engine = engine_for(store, chain, {TOKEN})
    run_all(follower, engine, {W1, W2, W3})
    muted_state = state(store)
    assert outbox(store) == []                                   # no trade, transfer, cluster
    assert [s[0] for s in muted_state["signals"]] == ["cluster_buy", "independent_buy"]
    # identical run without the mute on a fresh database: everything but the outbox is equal
    with store.tx() as c:
        c.execute(sa.text("TRUNCATE trades, wallet_positions, realized_pnl, signals, signal_trades, "
                          "paper_trades, notification_outbox, notification_deliveries, "
                          "token_transfers, transactions, indexed_blocks, indexer_checkpoints "
                          "RESTART IDENTITY CASCADE"))
    store2, follower2, engine2 = engine_for(store, chain, set())
    run_all(follower2, engine2, {W1, W2, W3})
    assert state(store2) == muted_state
    kinds = {r["dedup_key"].split(":")[0] for r in outbox(store2)}
    assert kinds == {"ev", "signal"}                             # the unmuted run does alert


def test_a_look_alike_usdc_and_other_tokens_still_alert(store, chain):
    store, follower, engine = engine_for(store, chain, {USDC, FAKE_USDC.replace("e", "d")})
    run_all(follower, engine, {W1, W2, W3})                      # TOKEN is not muted
    assert any(r["dedup_key"].startswith("ev:") for r in outbox(store))
    assert any(r["dedup_key"].startswith("signal:") for r in outbox(store))


def ev(kind, token, *, counter=None, tx="0x01", wallet=W1, hype=E18):
    return TradeEvent(kind=kind, tx=tx, block=10, wallet=wallet, token=token, qty=1000 * E18,
                      hype_wei=hype, counter_token=counter, counter_qty=5 * 10**6 if counter else None)


def apply(store, engine, events, cursor=10):
    engine.apply_range(ScanResult(events=events, block_times={10: 1_800_000_600}), cursor)


@pytest.fixture
def eng(store):
    acfg = AlertConfig(min_trade_hype=0.1, telegram_mute_tokens=[USDC])
    from agora_tracker.chain import TokenCache

    class FakeRpc:
        def call(self, method, params):
            data = params[0]["data"]
            sym = "USDC" if params[0]["to"] in (USDC, FAKE_USDC) else "TKX"
            if data == "0x95d89b41":
                b = sym.encode()
                return "0x" + (32).to_bytes(32, "big").hex() + len(b).to_bytes(32, "big").hex() + \
                    b.ljust(32, b"\0").hex()
            return hex(6 if sym == "USDC" else 18)

        def batch(self, calls):
            return [self.call(m, p) for m, p in calls]
    tc = TokenCache(FakeRpc(), store)
    tc.resolve({USDC, FAKE_USDC, TOKEN_X})
    return Engine(store, tc, ChainConfig(rpc_urls=["fake"]), acfg, {})


@pytest.mark.parametrize("kind", ["BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT", "SWAP", "MULTI"])
def test_every_usdc_alert_kind_is_suppressed_but_still_recorded(store, eng, kind):
    e = ev(kind, USDC, tx=f"0x{kind.lower()}")
    if kind == "MULTI":
        e.inbound = True
    apply(store, eng, [e])
    assert outbox(store) == []
    with store.tx() as c:
        assert c.execute(sa.text("SELECT count(*) FROM trades WHERE token = :t"),
                         {"t": USDC}).scalar() == 1


def test_a_token_bought_with_usdc_as_the_quote_asset_still_alerts(store, eng):
    apply(store, eng, [ev("BUY", TOKEN_X, counter=USDC, tx="0xq1"),
                       ev("SELL", USDC, counter=TOKEN_X, tx="0xq1")])     # the USDC leg itself
    rows = outbox(store)
    assert [r["payload"]["token"] for r in rows] == [TOKEN_X]
    assert rows[0]["payload"]["counter_token"] == USDC


def test_a_fake_usdc_with_the_same_symbol_still_alerts(store, eng):
    apply(store, eng, [ev("BUY", FAKE_USDC, tx="0xf1")])
    rows = outbox(store)
    assert len(rows) == 1 and rows[0]["payload"]["symbol"] == "USDC"
    assert rows[0]["payload"]["token"] == FAKE_USDC


def test_replay_of_a_muted_range_enqueues_nothing_and_counts_nothing_twice(store, eng):
    events = [ev("BUY", USDC, tx="0xr1"), ev("BUY", TOKEN_X, tx="0xr2")]
    apply(store, eng, events)
    before = (outbox(store), state(store))
    apply(store, eng, events)
    assert (outbox(store), state(store)) == before


# ------------------------------------------------------------------ queued before the deploy
def queue(store, key, token=None, *, status="pending", attempts=0, retry_later=False):
    payload = None if token is None else {"type": "trade", "token": token, "symbol": "USDC"}
    store.enqueue_now(key, f"text {key}", payload=payload)
    with store.tx() as c:
        c.execute(sa.text("UPDATE notification_outbox SET status = :s, attempts = :a, "
                          "next_attempt_at = now() + CASE WHEN :later THEN interval '1 hour' "
                          "ELSE interval '0' END WHERE dedup_key = :k"),
                  {"s": status, "a": attempts, "later": retry_later, "k": key})


def notifier(store, sent, suppress=(USDC,)):
    n = Notifier(store, TelegramConfig(bot_token="1:x", chat_id="1", min_interval_s=0),
                 suppress_tokens=suppress)
    n.session.post = lambda url, json, timeout: (sent.append(json["text"]), FakeResp(200))[1]
    return n


def test_alerts_queued_before_the_mute_are_closed_unsent_and_audited(store):
    queue(store, "ev:usdc-pending", USDC)
    queue(store, "ev:usdc-retrying", USDC_CHECKSUM, attempts=3, retry_later=True)  # case differs
    queue(store, "signal:usdc-cluster", USDC)
    queue(store, "ev:usdc-already-sent", USDC, status="sent")
    queue(store, "ev:usdc-already-dead", USDC, status="dead")
    queue(store, "ev:fake-usdc", FAKE_USDC)
    queue(store, "ev:token-x", TOKEN_X)
    queue(store, "lb:RANK:x:1")                                   # leaderboard: no payload
    sent: list[str] = []
    n = notifier(store, sent)
    assert n.drain() == 3
    assert sent == ["text ev:fake-usdc", "text ev:token-x", "text lb:RANK:x:1"]
    rows = {r["dedup_key"]: r for r in outbox(store)}
    for k in ("ev:usdc-pending", "ev:usdc-retrying", "signal:usdc-cluster"):
        assert rows[k]["status"] == "dead" and rows[k]["last_error"].startswith("suppressed:")
    assert rows["ev:usdc-already-sent"]["status"] == "sent"     # history untouched
    assert rows["ev:usdc-already-dead"]["last_error"] is None
    with store.tx() as c:
        aud = [tuple(r) for r in c.execute(sa.text(
            "SELECT o.dedup_key, d.ok, d.error FROM notification_deliveries d "
            "JOIN notification_outbox o ON o.id = d.outbox_id WHERE NOT d.ok ORDER BY o.dedup_key"))]
        sent_rows = c.execute(sa.text("SELECT count(*) FROM notification_outbox "
                                      "WHERE status = 'sent' AND last_error LIKE 'suppressed:%'")
                              ).scalar()
    assert [a[0] for a in aud] == ["ev:usdc-pending", "ev:usdc-retrying", "signal:usdc-cluster"]
    assert all(a[1] is False and a[2].startswith("suppressed:") for a in aud)
    assert sent_rows == 0                                         # never falsely "sent"
    counts = store.outbox_counts()
    assert counts["suppressed"] == 3 and counts["sent"] == 4 and counts["dead"] == 1
    # replay: a second drain neither re-sends nor re-audits anything
    assert n.drain() == 0 and len(sent) == 3
    with store.tx() as c:
        assert c.execute(sa.text("SELECT count(*) FROM notification_deliveries")).scalar() == 6


def test_an_alert_queued_after_the_bulk_pass_is_caught_at_claim_time(store, monkeypatch):
    queue(store, "ev:usdc-late", USDC)
    queue(store, "ev:token-x", TOKEN_X)
    sent: list[str] = []
    n = notifier(store, sent)
    monkeypatch.setattr(store, "suppress_pending_alerts", lambda *a: 0)   # race: missed by bulk
    assert n.drain() == 1 and sent == ["text ev:token-x"]
    rows = {r["dedup_key"]: r for r in outbox(store)}
    assert rows["ev:usdc-late"]["status"] == "dead"
    assert rows["ev:usdc-late"]["last_error"].startswith("suppressed:")


def test_without_a_mute_nothing_changes(store):
    queue(store, "ev:usdc", USDC)
    sent: list[str] = []
    assert notifier(store, sent, suppress=()).drain() == 1 and sent == ["text ev:usdc"]


@pytest.mark.parametrize("muted", [True, False])
def test_the_plain_cluster_alert_path_is_muted_too(store, chain, muted):
    """With the 'independent' (strong) signal disabled, the plain cluster card is the one that
    would be sent: muted -> recorded but not queued; unmuted -> queued."""
    from agora_tracker.config import IntelConfig
    store, follower, engine = engine_for(store, chain, {TOKEN} if muted else set())
    engine.intel = IntelConfig(independent_min=0)
    run_all(follower, engine, {W1, W2, W3})
    with store.tx() as c:
        kinds = [r[0] for r in c.execute(sa.text("SELECT kind FROM signals"))]
    assert kinds == ["cluster_buy"]                              # recorded either way
    clusters = [r for r in outbox(store) if r["dedup_key"].startswith("signal:")]
    assert (clusters == []) is muted
