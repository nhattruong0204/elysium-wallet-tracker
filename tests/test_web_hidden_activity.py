# ruff: noqa: F811
"""Web-only default-visibility filter for Elysium mainnet USDC (hotfix 2026-11-10).

The website hides one exact contract from the DEFAULT Activity feed (page, since_id poll,
count, cursor pagination) and the overview / wallet recent-trade lists. Everything else stays:
an explicit token filter, a full-address search, the token's own page, the rankings, the stored
rows, and the tracker's Telegram mute (a different, tracker-only setting)."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from conftest import CHAIN, TEST_URL
from test_activity import API, add, get, items, kinds  # noqa: F401
from test_dashboard_db import NOW, PRIV, PUB1, PUB2, TOK_PUB, seeded  # noqa: F401
from test_web import make_cfg, make_live  # noqa: F401

from agora_tracker import schema as t
from agora_tracker.config import load_config
from agora_tracker.web import activity as A
from agora_tracker.web.run import build

ROOT = Path(__file__).resolve().parent.parent
USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"          # Elysium mainnet (99901) USDC
USDC_MIXED = "0xb47c4C8F73F009CC0C0d3f92B388d13E293F0B65"
FAKE_USDC = "0x" + "5c" * 20                                 # another contract that says "USDC"
KINDS = ("BUY", "SELL", "SWAP", "TRANSFER_IN", "TRANSFER_OUT", "MULTI")


def hide_cfg(**web):
    return make_cfg(activity_hidden_tokens=[USDC], **web)


@pytest.fixture
def usdc(kinds):
    """`kinds` plus real USDC legs of every kind (interleaved in time with other tokens) and a
    fake 'USDC' at another address."""
    with kinds.tx() as c:
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=USDC, symbol="USDC",
                                                     name="USD Coin"))
        c.execute(sa.insert(t.token_profiles).values(chain_id=CHAIN, address=FAKE_USDC,
                                                     symbol="USDC", name="Totally USD Coin"))
    real, fake = [], []
    for i, kind in enumerate(KINDS * 3):
        hype = 1.5 if kind in ("BUY", "SELL") else None
        real.append(add(kinds, kind, f"0xd{i:02d}", token=USDC, hype=hype, seconds_ago=40 - i,
                        block=7000 + i, wallet=PUB1 if i % 2 else PUB2))
    for i in range(3):
        fake.append(add(kinds, "BUY", f"0xf{i:02d}", token=FAKE_USDC, hype=0.7,
                        seconds_ago=35 - i, block=7100 + i))
    kinds.real_usdc, kinds.fake_usdc = real, fake
    return kinds


def public_ids(engine, *, exclude=()):
    with engine.connect() as c:
        rows = c.execute(sa.text("SELECT id, token FROM public_api.trades WHERE chain_id = :c"),
                         {"c": CHAIN}).all()
    return {r.id for r in rows if r.token not in exclude}


# --- configuration -------------------------------------------------------------------------
def test_env_is_normalised_validated_and_separate_from_the_telegram_mute(monkeypatch, tmp_path):
    src = (ROOT / "config.example.yaml").read_text()
    p = tmp_path / "config.yaml"
    p.write_text(src)
    monkeypatch.delenv("ALERTS_TELEGRAM_MUTE_TOKENS", raising=False)
    monkeypatch.delenv("WEB_ACTIVITY_HIDDEN_TOKENS", raising=False)
    assert load_config(p).web.activity_hidden_tokens == []          # off by default
    monkeypatch.setenv("WEB_ACTIVITY_HIDDEN_TOKENS", f" {USDC_MIXED}, {USDC} ")
    cfg = load_config(p)
    assert cfg.web.activity_hidden_tokens == [USDC]                 # lower-cased, de-duplicated
    # the web setting never leaks into the tracker's alert settings (and vice versa)
    assert cfg.alerts.telegram_mute_tokens == [] and USDC not in cfg.alerts.mute_tokens
    assert USDC not in cfg.alerts.ignore_tokens
    monkeypatch.setenv("WEB_ACTIVITY_HIDDEN_TOKENS", "")
    monkeypatch.setenv("ALERTS_TELEGRAM_MUTE_TOKENS", USDC)
    cfg = load_config(p)
    assert cfg.web.activity_hidden_tokens == [] and cfg.alerts.telegram_mute_tokens == [USDC]
    for bad in ("USDC", "usdc.e", "0x123", USDC + "00"):              # never by symbol
        monkeypatch.setenv("WEB_ACTIVITY_HIDDEN_TOKENS", bad)
        with pytest.raises(ValueError):
            load_config(p)


def test_compose_sets_it_on_the_web_service_only():
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    env = {name: (s.get("environment") or {}) for name, s in svc.items()}
    assert env["web"]["WEB_ACTIVITY_HIDDEN_TOKENS"] == USDC
    for name in svc:
        if name != "web":
            assert "WEB_ACTIVITY_HIDDEN_TOKENS" not in env[name], name
    # PR #118's tracker-only Telegram mute is untouched and not given to the website
    assert env["tracker"]["ALERTS_TELEGRAM_MUTE_TOKENS"] == USDC
    assert "ALERTS_TELEGRAM_MUTE_TOKENS" not in env["web"]
    # the website still logs in as the read-only role and has no secrets file
    assert env["web"]["DATABASE_URL"].startswith("postgresql+psycopg://dashboard_ro:")
    assert "env_file" not in svc["web"]
    assert "WEB_ACTIVITY_HIDDEN_TOKENS" not in (ROOT / "deploy" / "config.yaml").read_text()


# --- the default feed --------------------------------------------------------------------------
def test_default_feed_hides_every_usdc_kind_and_keeps_everything_else(usdc, make_live):
    lv = make_live(hide_cfg())
    rows = items(lv, range="all", limit=100)
    toks = {r["token"] for r in rows}
    assert USDC not in toks
    assert {TOK_PUB, FAKE_USDC, "0x" + "2b" * 20} <= toks           # others + the look-alike
    fake = [r for r in rows if r["token"] == FAKE_USDC]
    assert len(fake) == 3 and all(r["symbol"] == "USDC" for r in fake)   # not hidden by symbol
    assert {r["id"] for r in rows} == public_ids(usdc.engine, exclude={USDC})
    # every action filter, including the transfer/swap/multi ones
    for action in ("buy", "sell", "swap", "transfer_in", "transfer_out", "multi", "transfers"):
        r = get(lv, range="all", limit=100, action=action)
        if r.status_code == 400:                  # an action name this build does not offer
            continue
        assert all(i["token"] != USDC for i in r.json()["items"]), action
    # and the server-rendered page
    html = lv.get("/activity", params={"range": "all"}).text
    assert USDC not in html.lower() and FAKE_USDC in html


def test_without_the_setting_nothing_changes(usdc, make_live):
    rows = items(make_live(), range="all", limit=100)
    assert {r["id"] for r in rows} == public_ids(usdc.engine)


def test_explicit_token_filter_and_address_search_still_show_usdc(usdc, make_live):
    lv = make_live(hide_cfg())
    only = items(lv, range="all", limit=100, token=USDC)
    assert sorted(r["id"] for r in only) == sorted(usdc.real_usdc)
    assert {r["kind"] for r in only} == set(KINDS)
    assert get(lv, range="all", token=USDC).json()["total"]["count"] == len(usdc.real_usdc)
    mixed = items(lv, range="all", limit=100, token=USDC_MIXED)        # case-insensitive input
    assert sorted(r["id"] for r in mixed) == sorted(usdc.real_usdc)
    found = items(lv, range="all", limit=100, q=USDC)                   # full-address search
    assert {r["token"] for r in found} == {USDC}
    # a symbol search is not an exact address: the real one stays hidden, the look-alike shows
    sym = items(lv, range="all", limit=100, q="usdc")
    assert {r["token"] for r in sym} == {FAKE_USDC}
    # the token's own page and API still list it
    assert lv.get(f"/tokens/{USDC}").status_code == 200
    tok = lv.get(f"/api/v1/tokens/{USDC}").json()
    assert tok["trades"]["items"] and all(r["token"] == USDC for r in tok["trades"]["items"])


def test_a_wallet_filter_alone_does_not_unhide(usdc, make_live):
    lv = make_live(hide_cfg())
    rows = items(lv, range="all", limit=100, wallet=PUB2)
    assert rows and all(r["token"] != USDC for r in rows)
    both = items(lv, range="all", limit=100, wallet=PUB2, token=USDC)
    assert both and all(r["token"] == USDC and r["wallet"] == PUB2 for r in both)


# --- counts, cursors, live polling -------------------------------------------------------------
def test_count_and_cursor_pagination_follow_the_filter(usdc, make_live):
    lv = make_live(hide_cfg())
    expected = public_ids(usdc.engine, exclude={USDC})
    first = lv.get(API, params={"range": "all", "limit": 4}).json()
    assert first["total"]["count"] == len(expected)
    pages, j = [], first
    while True:
        pages.append([r for r in j["items"]])
        assert all(r["token"] != USDC for r in j["items"])
        if not j["page"]["next_cursor"]:
            break
        j = lv.get(API, params={"range": "all", "limit": 4, "dir": "next",
                                "cursor": j["page"]["next_cursor"]}).json()
    flat = [r for p in pages for r in p]
    ids = [r["id"] for r in flat]
    assert len(ids) == len(set(ids)) and set(ids) == expected          # no gaps, no duplicates
    keys = [(r["block_time"], r["id"]) for r in flat]
    assert keys == sorted(keys, reverse=True)                          # newest first throughout
    assert all(len(p) == 4 for p in pages[:-1])                        # pages stay full
    # walking back from the last page returns exactly the same pages
    back, cur = [], j["page"]["prev_cursor"]
    while cur:
        jj = lv.get(API, params={"range": "all", "limit": 4, "dir": "prev", "cursor": cur}).json()
        back.insert(0, [r["id"] for r in jj["items"]])
        cur = jj["page"]["prev_cursor"]
    assert back == [[r["id"] for r in p] for p in pages[:-1]]
    # oldest-first and largest-buy sorts are filtered too
    for sort in ("oldest", "largest_buy"):
        assert all(r["token"] != USDC for r in items(lv, range="all", limit=100, sort=sort))


def test_live_since_id_poll_skips_usdc_but_still_advances(usdc, make_live):
    lv = make_live(hide_cfg(activity_cache_ttl_s=0))
    head = lv.get(API, params={"range": "all"}).json()["head_id"]
    a = add(usdc, "BUY", "0xe1", token=USDC, hype=2.0, seconds_ago=2, block=9100)
    b = add(usdc, "TRANSFER_OUT", "0xe2", token=USDC, hype=None, seconds_ago=2, block=9100)
    c = add(usdc, "SELL", "0xe3", token=TOK_PUB, hype=1.0, seconds_ago=1, block=9101)
    d = add(usdc, "BUY", "0xe4", token=FAKE_USDC, hype=1.0, seconds_ago=1, block=9101)
    inc = lv.get(API, params={"range": "all", "since_id": head}).json()
    assert [r["id"] for r in inc["items"]] == [d, c] and inc["count"] == 2
    assert inc["head_id"] == max(a, b, c, d)        # the poll cursor moves past hidden rows
    assert lv.get(API, params={"range": "all", "since_id": inc["head_id"]}).json()["items"] == []
    # a page showing only USDC polls USDC
    tok = lv.get(API, params={"range": "all", "since_id": head, "token": USDC}).json()
    assert sorted(r["id"] for r in tok["items"]) == sorted([a, b])
    # only hidden rows arrived: the poll is empty but the head still advances
    e = add(usdc, "SWAP", "0xe5", token=USDC, hype=None, seconds_ago=0, block=9102)
    j = lv.get(API, params={"range": "all", "since_id": inc["head_id"]}).json()
    assert j["items"] == [] and j["head_id"] == e


def test_no_stale_usdc_page_survives_the_restart(usdc, make_live):
    """The activity cache is per process, so the new container starts empty; and the ETag
    covers the item ids, so a browser revalidating an old page gets the new one (200)."""
    old = make_live()                                      # the pre-hotfix configuration
    r_old = old.get(API, params={"range": "all", "limit": 50})
    assert any(i["token"] == USDC for i in r_old.json()["items"])
    new = make_live(hide_cfg())                            # long cache TTL, as in production
    r_new = new.s.get(new.base + API, params={"range": "all", "limit": 50},
                      headers={"If-None-Match": r_old.headers["ETag"]})
    assert r_new.status_code == 200
    assert all(i["token"] != USDC for i in r_new.json()["items"])
    assert r_new.headers["ETag"] != r_old.headers["ETag"]
    assert r_new.headers["Cache-Control"] == "private, no-cache"       # never cached publicly


# --- other web feeds -------------------------------------------------------------------------------
def test_overview_and_wallet_previews_follow_the_policy_rankings_do_not(usdc, make_live):
    lv = make_live(hide_cfg())
    ov = lv.get("/api/v1/overview", params={"range": "24h"}).json()
    assert ov["recent"] and all(r["token"] != USDC for r in ov["recent"])
    assert any(r["token"] == FAKE_USDC for r in ov["recent"])
    html = lv.get("/", params={"range": "24h"}).text
    assert USDC not in html.split('id="rec"')[1].lower()
    w = lv.get(f"/api/v1/wallets/{PUB2}").json()
    assert w["trades"]["items"] and all(r["token"] != USDC for r in w["trades"]["items"])
    # analytical rankings are NOT filtered (financial semantics unchanged): same as without it
    # (a fixed window, so both instances answer exactly the same question)
    plain = make_live()
    win = {"range": "custom", "start": f"{NOW - timedelta(days=1):%Y-%m-%dT%H:%M}",
           "end": f"{NOW + timedelta(minutes=1):%Y-%m-%dT%H:%M}"}   # floor: still > NOW
    def figures(rows):          # the ranked numbers, not clocks/market lookups of the moment
        keep = ("token", "legs", "wallets", "buyers", "sellers", "q_buyers", "q_sellers",
                "buy_wei", "sell_wei", "q_buy_wei", "q_sell_wei", "net_wei", "q_net_wei")
        return [{k: r[k] for k in keep if k in r} for r in rows]

    tok_a = lv.get("/api/v1/tokens", params=win).json()["page"]["items"]
    tok_b = plain.get("/api/v1/tokens", params=win).json()["page"]["items"]
    assert figures(tok_a) == figures(tok_b) and any(r["token"] == USDC for r in tok_a)
    sm_a = lv.get("/api/v1/smart-money", params=win).json()["page"]["items"]
    sm_b = plain.get("/api/v1/smart-money", params=win).json()["page"]["items"]
    assert figures(sm_a) == figures(sm_b)
    ov_a = lv.get("/api/v1/overview", params=win).json()
    ov_b = plain.get("/api/v1/overview", params=win).json()
    assert ov_a["totals"] == ov_b["totals"]
    assert figures(ov_a["top_tokens"]) == figures(ov_b["top_tokens"])
    assert any(r["token"] == USDC for r in ov_a["top_tokens"])            # USDC still ranked
    assert all(r["token"] != USDC for r in ov_a["recent"])               # ... not in the preview


# --- storage and isolation -------------------------------------------------------------------------
def test_nothing_is_written_and_the_role_stays_read_only(usdc, make_live):
    def snapshot():
        with usdc.engine.connect() as c:
            return (c.execute(sa.text("SELECT count(*), coalesce(sum(id), 0) FROM trades")).one(),
                    c.execute(sa.text("SELECT count(*) FROM trades WHERE token = :u"),
                              {"u": USDC}).scalar(),
                    c.execute(sa.text("SELECT md5(string_agg(t::text, ',' ORDER BY wallet, token))"
                                      " FROM wallet_positions t")).scalar())
    before = snapshot()
    lv = make_live(hide_cfg())
    for path in ("/activity", API, "/", "/api/v1/overview", f"/api/v1/wallets/{PUB1}",
                 f"/api/v1/tokens/{USDC}", "/api/v1/tokens"):
        assert lv.get(path, params={"range": "all"} if path in ("/activity", API)
                      else {}).status_code == 200, path
    assert snapshot() == before and before[1] == len(usdc.real_usdc)
    # the website still refuses the tracker's admin URL, with the new setting as well
    with pytest.raises(SystemExit):
        build(hide_cfg(), TEST_URL)
    with lv.engine.connect() as c:
        with pytest.raises(sa.exc.DBAPIError):
            c.execute(sa.text("SELECT count(*) FROM public.trades"))


def test_filter_sql_unhides_only_the_named_token():
    now = NOW
    p = A.parse_activity({"range": "all"}, now)
    where, prm = A.filter_sql(p, hidden=(USDC, FAKE_USDC))
    assert "t.token <> ALL(:hidden)" in where and prm["hidden"] == [USDC, FAKE_USDC]
    p = A.parse_activity({"range": "all", "token": USDC}, now)
    _, prm = A.filter_sql(p, hidden=(USDC, FAKE_USDC))
    assert prm["hidden"] == [FAKE_USDC]
    p = A.parse_activity({"range": "all", "q": USDC}, now)
    _, prm = A.filter_sql(p, hidden=(USDC,))
    assert "hidden" not in prm
    p = A.parse_activity({"range": "all", "wallet": PRIV}, now)
    _, prm = A.filter_sql(p, hidden=(USDC,))
    assert prm["hidden"] == [USDC]


# --- the production verification script -------------------------------------------------------
def run_check(lv, *extra):
    import subprocess
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "web_activity_check.py"),
                           "--base", lv.base, "--hidden", USDC, "--range", "all", *extra],
                          capture_output=True, text=True, timeout=120)


def test_deploy_check_script_passes_after_and_reports_before(usdc, make_live):
    after = run_check(make_live(hide_cfg()), "--expect-hidden")
    assert after.returncode == 0, after.stdout + after.stderr
    assert "RESULT: PASS" in after.stdout and "hidden-token items 0" in after.stdout
    before = make_live()                                    # pre-hotfix: report only, exit 0
    r = run_check(before)
    assert r.returncode == 0 and "RESULT: FAIL" in r.stdout and "[info]" in r.stdout
    strict = run_check(before, "--expect-hidden")           # would block a broken deploy
    assert strict.returncode == 1 and "default page 1 has no hidden-token rows" in strict.stdout
