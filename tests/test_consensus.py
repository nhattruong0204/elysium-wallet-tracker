"""Consensus tab: the sliding-window sweep (pure) and the page/API against the public views."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from test_dashboard_db import (PRIV, PRIVATE_LABEL, PUB1, PUB2, TOK_PRIV, TOK_PUB,  # noqa: F401
                               seeded)
from test_web import make_cfg, make_live, web  # noqa: F401

from agora_tracker.web.consensus import (ConsensusParams, Leg, build_cards, parse_params,
                                         sweep)

T0 = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
E = 10**18
A = lambda n: "0x" + f"{n:02x}" * 20   # noqa: E731
W30 = timedelta(minutes=30)


def leg(w, hype, minute, block=None, tx=None):
    return Leg(A(w), int(hype * E), T0 + timedelta(minutes=minute),
               block if block is not None else 100 + minute, tx or f"0x{w:02x}{minute:04d}")


def run(legs, n=3, min_hype=1.0, bundles=(), independent=False):
    return sweep("tok", legs, window=W30, min_traders=n, min_wei=int(min_hype * E),
                 bundles=set(bundles), independent=independent)


def test_threshold_opens_on_the_nth_qualifying_buyer_and_keeps_joiners():
    evs = run([leg(1, 2, 0), leg(2, 1.5, 10), leg(3, 1, 20), leg(4, 3, 45), leg(5, 1, 90)])
    assert len(evs) == 2 or len(evs) == 1
    ev = evs[0]
    assert ev.trigger_at == T0 + timedelta(minutes=20) and ev.first_n == 3
    assert ev.members(E) == [A(1), A(2), A(3), A(4)]       # 45 min: within 30 of the last join
    assert ev.last_join == T0 + timedelta(minutes=45)
    assert all(A(5) not in e.members(E) for e in evs)      # 90 min: after the window closed


def test_buyers_outside_the_window_or_below_min_do_not_count():
    assert run([leg(1, 2, 0), leg(2, 2, 31), leg(3, 2, 62)]) == []           # never 3 in 30 min
    assert run([leg(1, 2, 0), leg(2, 0.4, 5), leg(3, 2, 10)]) == []           # one is dust


def test_sells_count_against_the_wallet():
    evs = run([leg(1, 2, 0), leg(1, -1.8, 5), leg(2, 2, 10), leg(3, 2, 15)])
    assert evs == []                                            # wallet 1 is net 0.2 at 15 min
    evs = run([leg(1, 2, 0), leg(2, 2, 10), leg(3, 2, 15), leg(1, -1.9, 20)])
    assert evs[0].members(E) == [A(2), A(3)]                    # dumped after the trigger


def test_independent_mode_collapses_same_block_and_bundles():
    same_block = [leg(1, 2, 0, block=500), leg(2, 2, 0, block=500), leg(3, 2, 1, block=501)]
    assert run(same_block) and not run(same_block, independent=True)
    spread = [leg(1, 2, 0), leg(2, 2, 5), leg(3, 2, 9)]
    assert run(spread, independent=True)
    assert not run(spread, independent=True, bundles={A(1), A(2)})


def test_build_cards_shape_and_range_cut():
    rows = []
    for w, h, m in ((1, 2, 0), (2, 2, 5), (3, 2, 9), (1, 2, 200), (2, 2, 205), (3, 2, 209)):
        rows.append({"token": A(9), "wallet": A(w), "kind": "BUY", "hype_wei": int(h * E),
                     "block_time": T0 + timedelta(minutes=m), "block_number": 1000 + m,
                     "tx_hash": f"0x{m:04d}"})
    p = ConsensusParams(min_traders=3, window_min=30, min_net_hype=1.0)
    cards = build_cards(rows, p, since=T0 + timedelta(minutes=100), bundles={A(1)})
    assert len(cards) == 1                                        # the first one is before since
    c = cards[0]
    assert c["traders"] == 3 and c["independent"] == 3 and c["bundled"]
    assert c["net_hype"] == 6 and c["trigger_tx"] == "0x0209"
    assert [m["wallet"] for m in c["members"]] == [A(1), A(2), A(3)]


def test_params_reject_anything_off_the_menu():
    p = parse_params({"traders": "999", "window": "7", "min_net": "abc", "range": "90d",
                      "launchpad": "x'; drop", "independent": "1"})
    assert p == ConsensusParams(independent=True)
    p = parse_params({"traders": "2", "window": "120", "min_net": "0.5", "range": "7d",
                      "launchpad": "ASCEND"})
    assert (p.min_traders, p.window_min, p.min_net_hype, p.range, p.launchpad) == \
        (2, 120, 0.5, "7d", "ascend")


# --- page and API against the seeded public views ----------------------------------------------
def test_consensus_page_and_api_show_public_wallets_only(web):
    q = {"traders": "2", "window": "120", "min_net": "0.5", "range": "24h"}
    r = web.get("/consensus", params=q)
    assert r.status_code == 200
    html = r.text
    assert "Consensus" in html and "Settings" in html and 'href="/consensus"' in html
    assert PUB1[:6] in html and PUB2[:6] in html and "BOXA" in html
    assert PRIV not in html and PRIVATE_LABEL not in html and TOK_PRIV not in html
    j = web.get("/api/v1/consensus", params=q).json()
    assert j["total"] == 1 and j["params"]["min_traders"] == 2
    card = j["cards"][0]
    assert card["token"] == TOK_PUB and card["traders"] == 2
    assert {m["wallet"] for m in card["members"]} == {PUB1, PUB2}      # PRIV's 50 HYPE is invisible
    body = web.get("/api/v1/consensus", params=q).text
    assert PRIV not in body and PRIVATE_LABEL not in body


def test_consensus_defaults_render_empty_state(web):
    r = web.get("/consensus")
    assert r.status_code == 200 and "No consensus with these settings" in r.text
    assert web.get("/consensus", params={"traders": "abc"}).status_code == 200
