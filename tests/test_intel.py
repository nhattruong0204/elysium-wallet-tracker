"""Intelligence Center: time ranges, reporting SQL, Telegram UI flows, exports.

Uses the PostgreSQL test database (TEST_DATABASE_URL) and a fake Telegram API; no network.
"""

from __future__ import annotations

import csv
import io
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
import pytest
import sqlalchemy as sa

from agora_tracker import schema as t
from agora_tracker import tgviews as V
from agora_tracker.classify import TradeEvent
from agora_tracker.config import (
    AlertConfig,
    ChainConfig,
    Config,
    LeaderboardConfig,
    TelegramConfig,
    UiConfig,
)
from agora_tracker.export import Exporter, ExportError, safe_text
from agora_tracker.leaderboard import Trader
from agora_tracker.reports import Reports
from agora_tracker.tgui import IntelBot, TgError
from agora_tracker.timerange import (
    RangeError,
    TimeRange,
    custom_range,
    from_pref,
    parse_local,
    preset_range,
)

UTC = timezone.utc
VN = ZoneInfo("Asia/Ho_Chi_Minh")
E18 = 10 ** 18
NOW = datetime.now(UTC).replace(microsecond=0)
Q1, Q2, Q3 = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20
U1, M1 = "0x" + "b1" * 20, "0x" + "c1" * 20
TA, TB, TC = "0x" + "1a" * 20, "0x" + "1b" * 20, "0x" + "1c" * 20
TD, TE = "0x" + "1d" * 20, "0x" + "1e" * 20
OWNER = 42


# --- time ranges ---------------------------------------------------------------------------
def test_presets_are_half_open_and_previous_is_adjacent():
    tr = preset_range("6h", NOW)
    assert tr.end == NOW and tr.end - tr.start == timedelta(hours=6) and tr.is_rolling
    prev = tr.previous()
    assert prev.end == tr.start and prev.end - prev.start == timedelta(hours=6)


def test_local_input_is_converted_to_utc():
    dt = parse_local("2026-11-01 08:00", VN)
    assert dt == datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    assert parse_local("01/11/2026 08:00", VN) == dt


@pytest.mark.parametrize("a,b,msg", [
    ("2026-11-02 10:00", "2026-11-02 09:00", "after the start"),
    ("2024-01-01 00:00", "2024-01-02 00:00", "no data exists"),
    ("2026-01-01 00:00", "2026-06-01 00:00", "longer than"),
])
def test_invalid_custom_ranges_are_rejected(a, b, msg):
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    with pytest.raises(RangeError, match=msg):
        custom_range(parse_local(a, VN), parse_local(b, VN), now)
    with pytest.raises(RangeError):
        parse_local("yesterday", VN)


def test_custom_range_is_fixed_and_bad_prefs_fall_back_to_24h():
    s, e = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 1, 6, tzinfo=UTC)
    tr = from_pref(None, s, e, NOW)
    assert (tr.start, tr.end, tr.preset) == (s, e, None)   # not moved to the present
    assert from_pref("bogus", None, None, NOW).preset == "24h"


# --- fixtures --------------------------------------------------------------------------------
def at(**kw) -> int:
    return int((NOW - timedelta(**kw)).timestamp())


@pytest.fixture
def seeded(store):
    with store.tx() as c:
        c.execute(sa.insert(t.token_metadata), [
            {"chain_id": 99901, "address": a, "symbol": s, "decimals": 18}
            for a, s in ((TA, "AAA"), (TB, "BBB"), (TC, "CCC"), (TD, "DUP"), (TE, "DUP"))])
        c.execute(sa.insert(t.token_profiles), [
            {"chain_id": 99901, "address": TA, "symbol": "AAA", "name": "Alpha",
             "launchpad": "boxa"},
            {"chain_id": 99901, "address": TB, "symbol": "BBB", "name": "Beta",
             "launchpad": "kleos"}])
    sid = store.record_snapshot(
        [Trader(address=Q1, rank=1, pnl=500.0), Trader(address=Q2, rank=2, pnl=200.0),
         Trader(address=Q3, rank=3, pnl=50.0), Trader(address=U1, rank=4, pnl=-5.0)],
        source="agora", status="ok", complete=True, row_count=4)
    with store.tx() as c:
        c.execute(sa.update(t.leaderboard_snapshots).where(t.leaderboard_snapshots.c.id == sid)
                  .values(captured_at=NOW - timedelta(hours=23)))
    legs = [  # (tx, wallet, token, kind, hype, conf, when)
        ("0x01", Q1, TA, "BUY", 2.0, "high", at(hours=5)),
        ("0x02", Q2, TA, "BUY", 1.0, "high", at(hours=1)),
        ("0x03", Q3, TA, "BUY", 0.5, "estimated", at(hours=2)),
        ("0x04", U1, TA, "BUY", 10.0, "high", at(hours=3)),
        ("0x05", Q1, TB, "SELL", 3.0, "high", at(hours=4)),
        ("0x06", Q2, TB, "SELL", 1.0, "high", at(hours=4)),
        ("0x07", Q3, TB, "BUY", 0.5, "high", at(minutes=30)),
        ("0x08", M1, TC, "TRANSFER_IN", None, "unknown", at(hours=2)),
        ("0x09", Q1, TA, "BUY", 0.4, "high", at(minutes=30)),
        ("0x10", Q1, TA, "BUY", 1.0, "high", at(hours=8)),           # previous window
    ]
    ids = {}
    with store.tx() as c:
        for i, (tx, w, tok, kind, h, conf, ts) in enumerate(legs):
            txh = tx.ljust(66, "0")
            ev = TradeEvent(kind, txh, 1000 + i, w, tok, 100 * E18,
                            None if h is None else int(Decimal(str(h)) * E18), confidence=conf,
                            hype_method="balance_diff" if conf == "high" else "none")
            ids[tx] = store.insert_trade(c, ev, ts)
        store.record_realized(c, ids["0x05"], Q1, TB, qty_sold=100 * E18, proceeds_wei=3 * E18,
                              cost_basis_wei=2 * E18, pnl_wei=1 * E18, status="measured")
        store.record_realized(c, ids["0x06"], Q2, TB, qty_sold=100 * E18, proceeds_wei=E18,
                              cost_basis_wei=12 * E18 // 10, pnl_wei=-2 * E18 // 10,
                              status="measured")
        store.set_cursor(c, 2000)
        c.execute(sa.insert(t.token_market_snapshots), [
            {"chain_id": 99901, "token": TA, "captured_at": NOW - timedelta(hours=6) + timedelta(minutes=1),
             "source": "agora", "price_hype": Decimal("1.0"), "mcap_hype": 1000, "vol_24h_hype": 100},
            {"chain_id": 99901, "token": TA, "captured_at": NOW - timedelta(minutes=1),
             "source": "agora", "price_hype": Decimal("1.5"), "mcap_hype": 1500, "vol_24h_hype": 100},
            {"chain_id": 99901, "token": TB, "captured_at": NOW - timedelta(hours=6) + timedelta(minutes=1),
             "source": "agora", "price_hype": Decimal("2"), "mcap_hype": 1000, "vol_24h_hype": 100},
            {"chain_id": 99901, "token": TB, "captured_at": NOW - timedelta(minutes=1),
             "source": "agora", "price_hype": Decimal("1"), "mcap_hype": 500, "vol_24h_hype": 100},
            {"chain_id": 99901, "token": TC, "captured_at": NOW - timedelta(minutes=1),
             "source": "agora", "price_hype": Decimal("9"), "mcap_hype": 500, "vol_24h_hype": 100},
        ])
    store.set_kv("heartbeat", str(int(NOW.timestamp())))
    return store


def ui_cfg(**kw) -> UiConfig:
    return UiConfig(admin_ids=[OWNER], min_volume_hype=1, min_mcap_hype=1, min_age_h=0, **kw)


@pytest.fixture
def reports(seeded):
    return Reports(seeded.engine, 99901, ui_cfg(), value_unit="HYPE", market=None)


W6 = preset_range("6h", NOW)


# --- reporting SQL ---------------------------------------------------------------------------
def test_overview_separates_verified_estimated_and_transfers(reports):
    o = reports.market_overview(W6)
    tt = o["totals"]
    assert tt["buy"] == Decimal("13.9")             # 2 + 1 + 10 + 0.5 + 0.4, verified only
    assert tt["sell"] == Decimal("4")
    assert tt["buy_est"] == Decimal("0.5")          # Q3's estimate never enters the totals
    assert tt["transfers"] == 1 and tt["buys"] == 5 and tt["sells"] == 2
    assert tt["wallets"] == 5 and tt["tokens"] == 3 and tt["new_tokens"] == 2  # TB, TC
    assert o["prev"]["buy"] == Decimal("1")         # previous 6h window
    assert any("increased" in s for s in o["summary"])
    pads = {p["launchpad"]: p for p in o["launchpads"]}
    assert pads["boxa"]["volume"] == Decimal("13.4") and "unknown" in pads


def test_smart_money_counts_only_qualified_verified_buys(reports):
    d = reports.smart_money(W6, "net")
    rows = {r["token"]: r for r in d["rows"]}
    assert d["universe"].basis == "leaderboard" and U1 not in d["universe"].qualified
    a = rows[TA]
    assert a["q_buyers"] == 2 and a["q_buy"] == Decimal("3.4") and a["buyers"] == 3
    assert rows[TB]["q_net"] == Decimal("-3.5")
    assert d["rows"][0]["token"] == TA
    assert [r["token"] for r in reports.smart_money(W6, "exits")["rows"]] == [TB]
    assert [r["token"] for r in reports.smart_money(W6, "repeat")["rows"]] == [TA]
    assert TC not in rows                                          # transfer != purchase


def test_unscored_when_no_leaderboard_snapshot(seeded, reports):
    with seeded.tx() as c:
        c.execute(sa.text("DELETE FROM leaderboard_snapshots"))
    d = reports.smart_money(W6, "buyers")
    assert d["universe"].basis == "unscored"
    assert {r["token"]: r["q_buyers"] for r in d["rows"]}[TA] == 3   # every tracked wallet


def test_traders_keep_agora_and_measured_pnl_apart(reports):
    d = reports.top_traders(W6, "realized")
    assert [r["wallet"] for r in d["rows"]] == [Q1, Q2]
    assert d["rows"][0]["realized"] == Decimal("1") and d["rows"][0]["agora_pnl"] == Decimal("500")
    assert [r["wallet"] for r in reports.top_traders(W6, "agora")["rows"]][:2] == [Q1, Q2]
    assert reports.top_traders(W6, "consistent")["rows"] == []        # < 3 measured sells


def test_wallet_and_token_detail(reports):
    w = reports.wallet_detail(Q1, W6)
    assert w["rank"] == 1 and w["buy"] == Decimal("2.4") and w["sell"] == Decimal("3")
    assert w["realized"]["measured"]["pnl"] == Decimal("1")
    b = reports.token_buyers(TA, W6)
    assert [r["wallet"] for r in b["rows"]][0] == U1 and not b["rows"][0]["qualified"]
    assert len(reports.trades(W6, token=TA, limit=100)) == 5


def test_search_address_symbol_and_disambiguation(reports):
    assert reports.search(TA.upper().replace("0X", "0x")) == {"kind": "token", "address": TA}
    assert reports.search(Q1)["kind"] == "wallet"
    assert reports.search("$aaa") == {"kind": "token", "address": TA}
    dup = reports.search("DUP")
    assert dup["kind"] == "tokens" and {r["address"] for r in dup["rows"]} == {TD, TE}
    assert reports.search("nothing")["kind"] == "none"


def test_coverage_reports_partial_history(reports):
    assert reports.coverage(preset_range("24h", NOW), NOW)["status"] == "partial"
    old = TimeRange(NOW - timedelta(days=10), NOW - timedelta(days=9))
    assert reports.coverage(old, NOW)["status"] == "none"


def test_gainers_losers_from_snapshots_need_both_ends(reports):
    up = reports.performance(W6, "up", "price", NOW)
    assert up["source"] == "snapshots"
    assert [(r["token"], round(r["ret"], 4)) for r in up["rows"]] == [(TA, 0.5)]
    assert up["rows"][0]["q_buyers"] == 2
    down = reports.performance(W6, "down", "price", NOW)
    assert [(r["token"], round(r["ret"], 4)) for r in down["rows"]] == [(TB, -0.5)]
    # A window the snapshots don't cover is reported unavailable, never filled in.
    old = TimeRange(NOW - timedelta(days=20), NOW - timedelta(days=19))
    res = reports.performance(old, "up", "price", NOW)
    assert res["rows"] == [] and "snapshots" in res["note"]
    flow = reports.performance(W6, "down", "flow", NOW)
    assert [r["token"] for r in flow["rows"]] == [TB]


def test_trending_historical_window_uses_tracked_evidence_only(reports):
    d = reports.trending(TimeRange(NOW - timedelta(hours=6), NOW), "score", NOW)
    assert not d["live"] and "tracked-wallet evidence" in d["note"]
    top = d["rows"][0]
    assert top["token"] == TA and top["score"] == round(2 / 5 * 30 + 3.4 / 20 * 15)
    assert any("2 qualified" in w for w in top["why"])


# --- exports ---------------------------------------------------------------------------------
def test_formula_injection_is_neutralized():
    assert safe_text("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert safe_text("@cmd") == "'@cmd" and safe_text("-1.5") == "-1.5"
    assert safe_text("BLOB") == "BLOB"


def test_xlsx_complete_report_is_valid_and_deterministic(reports, tmp_path):
    ex = Exporter(reports, VN, max_rows=1000, max_bytes=10_000_000)
    p1, name, rows = ex.write("full", W6, "xlsx", str(tmp_path))
    assert name.startswith("elysium_complete_report_") and name.endswith(".xlsx") and rows > 0
    wb = openpyxl.load_workbook(p1)
    assert wb.sheetnames == ["Summary", "Smart Money", "Trending", "Gainers", "Losers",
                             "Top Traders", "Transactions", "Data Quality"]
    tx = wb["Transactions"]
    assert tx.freeze_panes == "A2" and tx.auto_filter.ref
    hdr = [c.value for c in tx[1]]
    assert {"chain_id", "block_number", "block_time_utc", "tx_hash", "hype_wei",
            "confidence"} <= set(hdr)
    wei = {r[hdr.index("tx_hash")]: r[hdr.index("hype_wei")] for r in
           tx.iter_rows(min_row=2, values_only=True)}
    assert wei["0x09".ljust(66, "0")] == str(4 * E18 // 10)            # exact integer string
    first = [list(r) for r in wb["Smart Money"].iter_rows(values_only=True)]
    d2 = tmp_path / "again"
    d2.mkdir()
    p2, _, _ = ex.write("full", W6, "xlsx", str(d2))
    second = [list(r) for r in openpyxl.load_workbook(p2)["Smart Money"].iter_rows(values_only=True)]
    assert first == second


def test_csv_exports_single_and_zip(reports, tmp_path):
    ex = Exporter(reports, VN, max_rows=2, max_bytes=10_000_000)
    p, name, _ = ex.write("wallet", W6, "csv", str(tmp_path), subject=Q1)
    text = open(p, encoding="utf-8-sig").read()
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:3] == ["chain_id", "block_number", "block_time_utc"]
    assert rows[-1][0].startswith("# truncated at 2")                  # row limit honoured
    p, name, _ = ex.write("full", W6, "csv", str(tmp_path))
    assert name.endswith(".zip")
    assert "data_quality.csv" in zipfile.ZipFile(p).namelist()


def test_export_size_limit_and_empty_window(reports, tmp_path):
    with pytest.raises(ExportError, match="limit"):
        Exporter(reports, VN, max_rows=1000, max_bytes=100).write("full", W6, "xlsx",
                                                                  str(tmp_path))
    empty = TimeRange(NOW - timedelta(days=30), NOW - timedelta(days=29))
    p, _, _ = Exporter(reports, VN, max_rows=1000, max_bytes=10**7).write(
        "smart", empty, "xlsx", str(tmp_path))
    assert openpyxl.load_workbook(p)["Smart Money"].max_row == 1       # headers only


# --- Telegram UI ---------------------------------------------------------------------------
class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.docs: list[dict] = []
        self.fail_edit = False
        self.lock = threading.Lock()

    def call(self, method, timeout=None, **p):
        with self.lock:
            self.calls.append((method, p))
        if method == "editMessageText" and self.fail_edit:
            raise TgError(400, "Bad Request: message to edit not found")
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        return True

    def send_document(self, chat_id, path, filename, caption, reply_markup=None):
        with open(path, "rb") as f:
            self.docs.append({"chat": chat_id, "name": filename, "bytes": f.read(),
                              "caption": caption})

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def bot(seeded):
    cfg = Config(chain=ChainConfig(rpc_urls=["x"]), leaderboard=LeaderboardConfig(value_unit="HYPE"),
                 alerts=AlertConfig(), telegram=TelegramConfig(bot_token="1:x", chat_id="42"),
                 ui=ui_cfg())
    api = FakeApi()
    rep = Reports(seeded.engine, 99901, cfg.ui, value_unit="HYPE", market=None)
    b = IntelBot(cfg, api, seeded, rep, market=None, now=lambda: NOW)
    yield b
    b.shutdown()


def msg(text, uid=OWNER):
    return {"update_id": 1, "message": {"message_id": 5, "from": {"id": uid},
                                        "chat": {"id": uid}, "text": text}}


def cb(data, uid=OWNER):
    return {"update_id": 2, "callback_query": {"id": "q1", "from": {"id": uid}, "data": data,
                                               "message": {"message_id": 9, "chat": {"id": uid}}}}


def all_callbacks(markup):
    return [b["callback_data"] for row in (markup or {}).get("inline_keyboard", [])
            for b in row if "callback_data" in b]


def test_start_shows_persistent_keyboard_and_full_menu(bot):
    bot.handle(msg("/start"))
    sends = bot.api.of("sendMessage")
    assert sends[0]["reply_markup"]["is_persistent"] is True
    labels = [b["text"] for row in sends[0]["reply_markup"]["keyboard"] for b in row]
    assert labels == [label for row in V.MENU for label, _ in row]
    assert "ELYSIUM INTELLIGENCE CENTER" in sends[1]["text"]
    assert len(all_callbacks(sends[1]["reply_markup"])) == 10


def test_setup_registers_commands_and_recovers_interrupted_exports(bot, seeded):
    with seeded.tx() as c:
        c.execute(sa.insert(t.report_jobs).values(user_id=OWNER, kind="smart", fmt="xlsx",
                                                  range_start=NOW, range_end=NOW,
                                                  status="running"))
    bot.setup()
    cmds = bot.api.of("setMyCommands")[0]["commands"]
    assert {"start", "menu", "status", "export"} <= {c["command"] for c in cmds}
    with seeded.engine.connect() as c:
        assert c.execute(sa.text("SELECT status FROM report_jobs")).scalar() == "failed"
    assert "interrupted" in bot.api.of("sendMessage")[-1]["text"]


@pytest.mark.parametrize("data", [cb for row in V.MENU for _, cb in row])
def test_every_main_menu_button_opens_a_view(bot, data):
    bot.handle(cb(data))
    assert bot.api.of("answerCallbackQuery")                      # always acknowledged
    edits = bot.api.of("editMessageText")
    assert edits and edits[-1]["message_id"] == 9 and len(edits[-1]["text"]) < 4096
    for d in all_callbacks(edits[-1]["reply_markup"]):
        assert len(d.encode()) <= 64


def test_drill_downs_pagination_and_watch_toggle(bot, seeded):
    for d in ("sm|exits|0", "sm|clusters|0", "mo|cmp", f"tk|{TA}", f"tb|{TA}|0", f"tx|{TA}|0",
              f"w|{Q1}", f"wt|{Q1}|1", "tt|realized|0", "tt|movers|0", "gl|up|flow|0",
              "gl|down|price|0", "tr|new|0"):
        bot.handle(cb(d))
        assert "Something went wrong" not in str(bot.api.of("answerCallbackQuery")[-1])
    assert "Alpha" in bot.api.of("editMessageText")[3]["text"]       # token detail
    bot.handle(cb(f"ww|{U1}|1"))
    assert U1 in seeded.watchlist_addresses("telegram")
    bot.handle(cb(f"ww|{U1}|0"))
    assert U1 not in seeded.watchlist_addresses("telegram")


def test_pager_buttons():
    row = V.pager("sm|net", 1, 12, 5)
    assert [b["callback_data"] for b in row] == ["sm|net|0", "n", "sm|net|2"]
    assert V.pager("sm|net", 0, 3, 5) == []


def test_expired_and_malformed_callbacks_fall_back_home(bot):
    for d in ("zz|1", "sm|bogus|0", "tk|0xnotanaddress", "sm|net|99999", "x|f|xlsx"):
        bot.api.calls.clear()
        bot.handle(cb(d))
        assert "expired" in bot.api.of("answerCallbackQuery")[0]["text"]
        assert "INTELLIGENCE CENTER" in bot.api.of("editMessageText")[-1]["text"]


def test_edit_failure_falls_back_to_new_message(bot):
    bot.api.fail_edit = True
    bot.handle(cb("hl"))
    assert "SYSTEM HEALTH" in bot.api.of("sendMessage")[-1]["text"]


def test_unauthorized_users_get_no_data(bot):
    bot.handle(msg("/menu", uid=7))
    bot.handle(cb("sm|net|0", uid=7))
    texts = [p["text"] for p in bot.api.of("sendMessage")]
    assert len(texts) == 1 and "private" in texts[0]                # one refusal per hour
    assert bot.api.of("answerCallbackQuery")[0]["text"] == "Not authorized"
    assert not bot.api.of("editMessageText")


def test_period_selection_persists_across_views(bot):
    bot.handle(cb("sm|net|0"))
    bot.handle(cb("rg|6h"))
    text = bot.api.of("editMessageText")[-1]["text"]
    assert "SMART MONEY" in text and "Last 6 hours" in text           # returns to last view
    bot.handle(cb("tt|agora|0"))
    assert "Last 6 hours" in bot.api.of("editMessageText")[-1]["text"]
    assert bot.range_for(bot.sessions.get(OWNER)) == W6


def test_custom_range_conversation(bot):
    bot.handle(cb("mo"))
    bot.handle(cb("rg|c"))
    bot.handle(msg("not a date"))
    bot.handle(msg("2026-11-01 08:00"))
    assert "Couldn" not in bot.api.of("sendMessage")[-1]["text"]
    assert "END" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg("2026-10-31 08:00"))                                # before the start
    assert "after the start" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg("2026-11-01 20:00"))
    last = bot.api.of("sendMessage")[-1]["text"]
    assert "MARKET OVERVIEW" in last and "Custom period set" in last
    tr = bot.range_for(bot.sessions.get(OWNER))
    assert tr.start == datetime(2026, 11, 1, 1, tzinfo=UTC) and tr.preset is None


def test_typed_commands_search_and_reply_keyboard_text(bot):
    bot.handle(msg("/smart"))
    assert "SMART MONEY" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg("🐳 Top Traders"))
    assert "TOP TRADERS" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg(TB))
    assert "BBB" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg("DUP"))
    assert "2 tokens use the symbol" in bot.api.of("sendMessage")[-1]["text"]


def wait_reports(bot):
    bot.report_pool.submit(lambda: None).result(timeout=60)


def test_export_flow_sends_a_valid_xlsx(bot, seeded):
    bot.handle(cb("x"))
    bot.handle(cb("x|t|smart"))
    bot.handle(cb("x|r|6h"))
    bot.handle(cb("x|f|xlsx"))
    wait_reports(bot)
    assert len(bot.api.docs) == 1
    doc = bot.api.docs[0]
    assert doc["name"].startswith("elysium_smart_money_") and doc["name"].endswith(".xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(doc["bytes"]))
    sm = list(wb["Smart Money"].iter_rows(values_only=True))
    assert sm[0][9] == "qualified_buy_hype" and sm[1][1] == TA and float(sm[1][9]) == 3.4
    with seeded.engine.connect() as c:
        assert c.execute(sa.text("SELECT status, rows FROM report_jobs")).one()[0] == "done"
    assert "Sent" in bot.api.of("editMessageText")[-1]["text"]


def test_wallet_export_asks_for_subject_and_concurrent_exports_are_limited(bot, seeded):
    bot.handle(cb("x|t|wallet"))
    bot.handle(msg("nope"))
    assert "full 0x" in bot.api.of("sendMessage")[-1]["text"]
    bot.handle(msg(Q1))
    bot.handle(cb("x|r|cur"))
    gate = threading.Event()
    real = bot.exporter.write

    def slow(*a, **kw):
        gate.wait(10)
        return real(*a, **kw)
    bot.exporter.write = slow
    bot.handle(cb("x|f|csv"))
    bot.handle(cb("x|f|csv"))                                          # second while running
    assert "already have an export running" in bot.api.of("editMessageText")[-1]["text"]
    gate.set()
    wait_reports(bot)
    assert len(bot.api.docs) == 1 and bot.api.docs[0]["name"].endswith(".csv")
    assert Q1 in bot.api.docs[0]["bytes"].decode()


def test_tracker_follows_wallets_added_from_telegram(store, monkeypatch):
    from tests.test_p0_regressions import make_tracker
    tr = make_tracker(store, monkeypatch)
    tr.cfg.exclude_contracts = False
    store.watch_add(U1, source="telegram")
    tr.refresh_leaderboard()
    assert U1 in tr.tracked
