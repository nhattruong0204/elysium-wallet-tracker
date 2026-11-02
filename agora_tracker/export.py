"""CSV / XLSX report export built from reports.Reports (read-only PostgreSQL queries).

Guarantees:
  * Real persisted data only; for a fixed dataset and window the data rows are identical on
    every run (stable ORDER BY with tie-breakers). Only the "generated at" metadata changes.
  * Exact precision: every wei amount is also written as an integer string column
    (hype_wei); HYPE columns are Decimals (Excel itself displays ~15 significant digits).
  * Timestamps are UTC (column suffix _utc); the display zone is stated in the metadata.
  * Formula injection: text cells starting with = + - @ tab or CR get a leading apostrophe.
  * Bounded: rows per table <= ui.report_max_rows, file size <= ui.report_max_bytes;
    transactions are fetched in pages, never all at once.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .reports import Reports
from .timerange import TimeRange, tz_label

UTC = timezone.utc
KIND_FILE = {"market": "market_overview", "smart": "smart_money", "trending": "trending",
             "perf": "gainers_losers", "traders": "top_traders", "wallet": "wallet",
             "token": "token", "full": "complete_report"}
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


class ExportError(Exception):
    """User-facing failure (message is safe to show)."""


EXPORT_ERRORS = (ExportError,)


@dataclass
class Table:
    name: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False


def safe_text(v: Any) -> Any:
    """Neutralize spreadsheet formulas in text. Numbers are left alone."""
    if isinstance(v, str) and v and v.startswith(_DANGEROUS) and not _NUMERIC.match(v):
        return "'" + v
    return v


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(UTC).replace(tzinfo=None)
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if isinstance(v, (list, dict)):
        return safe_text("; ".join(map(str, v)) if isinstance(v, list) else str(v))
    return safe_text(v)


def _csv_value(v: Any) -> str:
    v = _cell(v)
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


class Exporter:
    def __init__(self, reports: Reports, tz: ZoneInfo, *, max_rows: int, max_bytes: int):
        self.r, self.tz = reports, tz
        self.max_rows, self.max_bytes = max_rows, max_bytes

    # --- datasets ------------------------------------------------------------------------
    def tables(self, kind: str, tr: TimeRange, subject: str | None = None) -> list[Table]:
        if kind == "market":
            return [self._summary(tr), self._flows(tr)]
        if kind == "smart":
            return [self._smart(tr), self._clusters(tr)]
        if kind == "trending":
            return [self._trending(tr)]
        if kind == "perf":
            return [self._perf(tr, "up"), self._perf(tr, "down")]
        if kind == "traders":
            return [self._traders(tr)]
        if kind == "wallet":
            if not subject:
                raise ExportError("no wallet address given")
            return [self._transactions(tr, wallet=subject)]
        if kind == "token":
            if not subject:
                raise ExportError("no token address given")
            return [self._buyers(tr, subject), self._transactions(tr, token=subject)]
        if kind == "full":
            return [self._summary(tr), self._smart(tr), self._trending(tr),
                    self._perf(tr, "up", "Gainers"), self._perf(tr, "down", "Losers"),
                    self._traders(tr), self._transactions(tr), self._quality(tr)]
        raise ExportError(f"unknown report type {kind!r}")

    def _limit(self, t: Table, rows: list[list[Any]]) -> Table:
        if len(rows) > self.max_rows:
            t.rows, t.truncated = rows[:self.max_rows], True
        else:
            t.rows = rows
        return t

    def _summary(self, tr: TimeRange) -> Table:
        o = self.r.market_overview(tr)
        cov = self.r.coverage(tr)
        t, p = o["totals"], o["prev"]
        rows: list[list[Any]] = [
            ["Report window start (UTC)", tr.start, ""],
            ["Report window end (UTC, exclusive)", tr.end, ""],
            ["Display timezone", tz_label(self.tz, tr.end), ""],
            ["Previous window start (UTC)", o["previous"].start, ""],
            ["Coverage", cov["status"], "; ".join(cov["warnings"])],
            ["Indexed to block", cov["cursor"], ""],
            ["Population", "tracked wallets (Agora top-N PnL + watchlist); not chain-wide", ""],
            ["Qualified wallets basis", o["universe"].basis,
             f"snapshot {o['universe'].captured_at}" if o["universe"].captured_at else ""],
            ["", "current", "previous"],
            ["Verified volume (HYPE)", t["volume"], p["volume"]],
            ["Verified buy volume (HYPE)", t["buy"], p["buy"]],
            ["Verified sell volume (HYPE)", t["sell"], p["sell"]],
            ["Net verified (HYPE)", t["net"], p["net"]],
            ["Qualified net verified (HYPE)", t["q_buy"] - t["q_sell"], p["q_buy"] - p["q_sell"]],
            ["Estimated buy (HYPE, not in totals)", t["buy_est"], p["buy_est"]],
            ["Estimated sell (HYPE, not in totals)", t["sell_est"], p["sell_est"]],
            ["Trade legs", t["legs"], p["legs"]],
            ["Verified BUY count", t["buys"], p["buys"]],
            ["Verified SELL count", t["sells"], p["sells"]],
            ["Estimated legs", t["est_legs"], p["est_legs"]],
            ["Transfers / other legs", t["transfers"] + t["other_legs"],
             p["transfers"] + p["other_legs"]],
            ["Active tracked wallets", t["wallets"], p["wallets"]],
            ["Tokens traded", t["tokens"], p["tokens"]],
            ["Tokens first seen in window", t["new_tokens"], ""],
            ["Smart-money signals", t["signals"], ""],
            ["Top-3 token share of verified volume", o["concentration_top3"], ""],
            ["Summary", " ".join(o["summary"]), ""],
            ["Generated at (UTC)", datetime.now(UTC).replace(microsecond=0), ""],
        ]
        return Table("Summary", ["Metric", "Value", "Previous / note"], rows)

    def _flow_row(self, r: dict) -> list[Any]:
        return [r["token"], r.get("symbol"), r.get("name"), r.get("launchpad"), r["q_buyers"],
                r["q_sellers"], r["buyers"], r["sellers"], r["q_buy"], r["q_sell"], r["q_net"],
                r["buy"], r["sell"], r["net"], r["buy_est"], r["sell_est"], r["buys"],
                r["sells"], r["est_legs"], r["transfers"], r["other_legs"],
                r["q_buy_intervals"], r["first_at"], r["last_at"]]

    FLOW_HEADERS = ["token", "symbol", "name", "launchpad", "qualified_buyers",
                    "qualified_sellers", "tracked_buyers", "tracked_sellers",
                    "qualified_buy_hype", "qualified_sell_hype", "qualified_net_hype",
                    "verified_buy_hype", "verified_sell_hype", "verified_net_hype",
                    "estimated_buy_hype", "estimated_sell_hype", "verified_buys",
                    "verified_sells", "estimated_legs", "transfers", "other_legs",
                    "qualified_buy_subintervals_of_4", "first_trade_utc", "last_trade_utc"]

    def _flows(self, tr: TimeRange) -> Table:
        d = self.r.smart_money(tr, "net")
        with self.r.ro() as c:
            flows = self.r.token_flows(c, tr, d["universe"].qualified)
            self.r._decorate(c, flows)
        flows.sort(key=lambda r: (-(r["buy"] + r["sell"]), r["token"]))
        return self._limit(Table("Token Flows", self.FLOW_HEADERS),
                           [self._flow_row(r) for r in flows])

    def _smart(self, tr: TimeRange) -> Table:
        d = self.r.smart_money(tr, "net")
        return self._limit(Table("Smart Money", ["rank"] + self.FLOW_HEADERS),
                           [[i] + self._flow_row(r) for i, r in enumerate(d["rows"], 1)])

    def _clusters(self, tr: TimeRange) -> Table:
        d = self.r.smart_money(tr, "clusters")
        return self._limit(Table("Clusters", ["signal_id", "token", "symbol", "kind",
                                              "wallets", "window_start_utc",
                                              "window_end_utc", "created_utc"]),
                           [[r["id"], r["token"], r.get("symbol"), r["kind"], r["n_wallets"],
                             r["window_start"], r["window_end"], r["created_at"]]
                            for r in d["rows"]])

    def _trending(self, tr: TimeRange) -> Table:
        d = self.r.trending(tr, "score")
        comps = ["smart_buyers", "smart_net", "acceleration", "buy_pressure", "frequency",
                 "recency", "momentum", "wash_penalty"]
        rows = [[i, r["token"], r.get("symbol"), r.get("name"), r.get("launchpad"), r["score"],
                 *[round(r["components"].get(k, 0.0), 2) for k in comps], r["q_buyers"],
                 r["q_net"], r.get("mcap"),
                 datetime.fromtimestamp(r["created_at"], UTC) if r.get("created_at") else None,
                 " | ".join(r["why"]), " | ".join(r["flags"])]
                for i, r in enumerate(d["rows"], 1)]
        return self._limit(Table("Trending", ["rank", "token", "symbol", "name", "launchpad",
                                              "score_0_100", *[f"pts_{k}" for k in comps],
                                              "qualified_buyers", "qualified_net_hype",
                                              "mcap_hype_now", "launched_utc", "why",
                                              "flags"]), rows)

    def _perf(self, tr: TimeRange, direction: str, name: str | None = None) -> Table:
        d = self.r.performance(tr, direction, "price")
        rows = [[i, r["token"], r.get("symbol"), r.get("name"), r.get("launchpad"), r["ret"],
                 r.get("price_start"), r.get("price_end"), r.get("volume"), r.get("mcap"),
                 r.get("q_buyers"), r.get("q_sellers"), d.get("source"), d.get("note")]
                for i, r in enumerate(d["rows"], 1)]
        t = Table(name or ("Gainers" if direction == "up" else "Losers"),
                  ["rank", "token", "symbol", "name", "launchpad", "return", "price_start_hype",
                   "price_end_hype", "volume_hype", "mcap_hype", "qualified_buyers",
                   "qualified_sellers", "source", "method"])
        if not rows:
            t.rows = [[None, None, None, None, None, None, None, None, None, None, None, None,
                       d.get("source"), d.get("note") or "no data"]]
            return t
        return self._limit(t, rows)

    def _traders(self, tr: TimeRange) -> Table:
        d = self.r.top_traders(tr, "all", limit=self.max_rows + 1)
        rows = [[r["wallet"], r["rank"], r["agora_pnl"], r["legs"], r["buys"], r["sells"],
                 r["tokens_bought"], r["buy"], r["realized"], r["n_measured"], r["wins"],
                 r["realized_est"], r["n_other"], r["last_at"]] for r in d["rows"]]
        return self._limit(Table("Top Traders", [
            "wallet", "agora_rank", f"agora_pnl_{self.r.unit.lower()}", "legs", "verified_buys",
            "verified_sells", "tokens_bought", "verified_buy_hype", "realized_measured_hype",
            "measured_sells", "winning_sells", "realized_estimated_hype",
            "sells_not_measured", "last_trade_utc"]), rows)

    TX_HEADERS = ["chain_id", "block_number", "block_time_utc", "tx_hash", "wallet", "token",
                  "symbol", "classification", "confidence", "hype_method", "qty_raw",
                  "qty", "hype_wei", "hype", "counter_token", "counter_qty_raw", "source"]

    def _transactions(self, tr: TimeRange, *, wallet: str | None = None,
                      token: str | None = None) -> Table:
        t = Table("Transactions", self.TX_HEADERS)
        rows: list[list[Any]] = []
        page = 5000
        off = 0
        while len(rows) <= self.max_rows:
            batch = self.r.trades(tr, wallet=wallet, token=token, limit=page, offset=off)
            for r in batch:
                rows.append([r["chain_id"], r["block_number"], r["block_time"], r["tx_hash"],
                             r["wallet"], r["token"], r["symbol"], r["kind"], r["confidence"],
                             r["hype_method"], str(int(r["qty"])), r["qty_units"],
                             None if r["hype_wei"] is None else str(int(r["hype_wei"])),
                             r["hype"], r["counter_token"],
                             None if r["counter_qty"] is None else str(int(r["counter_qty"])),
                             "on-chain (Elysium RPC) via elysium-wallet-tracker"])
            if len(batch) < page:
                break
            off += page
        return self._limit(t, rows)

    def _buyers(self, tr: TimeRange, token: str) -> Table:
        d = self.r.token_buyers(token, tr)
        rows = [[r["wallet"], r["rank"], r["agora_pnl"], r["qualified"], r["buys"], r["sells"],
                 r["buy"], r["sell"], r["est"], r["transfers"], r["first_buy"], r["last_at"]]
                for r in d["rows"]]
        return self._limit(Table("Token Wallets", [
            "wallet", "agora_rank", "agora_pnl", "qualified", "verified_buys", "verified_sells",
            "verified_buy_hype", "verified_sell_hype", "estimated_hype", "transfers",
            "first_buy_utc", "last_trade_utc"]), rows)

    def _quality(self, tr: TimeRange) -> Table:
        cov = self.r.coverage(tr)
        h = self.r.health()
        with self.r.ro() as c:
            conf = self.r._all(c, """
                SELECT kind, confidence, hype_method, count(*) AS n,
                       coalesce(sum(hype_wei), 0) AS wei
                FROM trades WHERE chain_id = :c AND block_time >= :s AND block_time < :e
                GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", s=tr.start, e=tr.end)
        rows: list[list[Any]] = [
            ["coverage", "status", cov["status"], "; ".join(cov["warnings"])],
            ["coverage", "tracking since (UTC)", cov["tracking_since"], ""],
            ["coverage", "indexed through (UTC)", cov["indexed_through"], ""],
            ["coverage", "indexer lag (blocks)", cov["lag"], ""],
            ["source", "leaderboard", "Agora /api/traders",
             f"last ok snapshot {h.get('lb_ok')}"],
            ["source", "market snapshots", "Agora /api/tokens",
             f"{h.get('market_since')} .. {h.get('market_at')}"],
            ["source", "trades", "Elysium RPC eth_getLogs + receipts + balance diffs", ""],
            ["convention", "interval", "[start, end)", "start inclusive, end exclusive"],
            ["convention", "timestamps", "UTC", f"display zone {self.tz.key}"],
            ["convention", "verified", "confidence = high",
             "HYPE leg measured on-chain; estimated amounts are separate columns"],
            ["convention", "precision", "hype_wei / qty_raw are exact integers",
             "HYPE columns are exact decimals in the file; Excel shows ~15 digits"],
            ["convention", "population", "tracked wallets only",
             "Agora top-N PnL leaderboard + watchlist at the time; not chain-wide"],
            ["convention", "PnL", "Agora PnL and measured realized PnL are separate",
             "never summed"],
            ["limits", "rows per table", self.max_rows, "tables marked truncated if exceeded"],
        ]
        for r in conf:
            rows.append(["trades in window", f"{r['kind']} / {r['confidence']} / "
                         f"{r['hype_method']}", r["n"], f"{Decimal(r['wei']) / 10**18} HYPE"])
        return Table("Data Quality", ["area", "item", "value", "detail"], rows)

    # --- writers -------------------------------------------------------------------------------
    def file_name(self, kind: str, tr: TimeRange, fmt: str, subject: str | None) -> str:
        base = KIND_FILE[kind]
        if subject:
            base += "_" + subject[:10]
        ext = "zip" if (fmt == "csv" and kind in ("full", "market", "smart", "perf", "token")) \
            else fmt
        return f"elysium_{base}_{tr.file_tag(self.tz)}.{ext}"

    def write(self, kind: str, tr: TimeRange, fmt: str, directory: str,
              subject: str | None = None) -> tuple[str, str, int]:
        """Build the report into `directory`. Returns (path, file name, data rows)."""
        tables = self.tables(kind, tr, subject)
        name = self.file_name(kind, tr, fmt, subject)
        path = os.path.join(directory, name)
        if fmt == "xlsx":
            self._xlsx(tables, path, kind, tr)
        elif fmt == "csv":
            self._csv(tables, path)
        else:
            raise ExportError(f"unsupported format {fmt!r}")
        size = os.path.getsize(path)
        if size > self.max_bytes:
            os.remove(path)
            raise ExportError(f"the file would be {size / 1e6:.1f} MB (limit "
                              f"{self.max_bytes / 1e6:.0f} MB); choose a shorter period")
        return path, name, sum(len(t.rows) for t in tables)

    def _csv(self, tables: list[Table], path: str) -> None:
        def render(t: Table) -> str:
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(t.headers)
            for row in t.rows:
                w.writerow([_csv_value(v) for v in row])
            if t.truncated:
                w.writerow([f"# truncated at {self.max_rows} rows"])
            return buf.getvalue()
        if path.endswith(".zip"):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for t in tables:
                    info = zipfile.ZipInfo(re.sub(r"\W+", "_", t.name).lower() + ".csv",
                                           date_time=(2026, 1, 1, 0, 0, 0))  # deterministic
                    z.writestr(info, render(t), compress_type=zipfile.ZIP_DEFLATED)
        else:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(render(tables[0]))

    def _xlsx(self, tables: list[Table], path: str, kind: str, tr: TimeRange) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook(write_only=False)
        wb.remove(wb.active)
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="1F3A5F")
        for t in tables:
            ws = wb.create_sheet(t.name[:31])
            ws.append(t.headers)
            for c in ws[1]:
                c.font, c.fill = head_font, head_fill
                c.alignment = Alignment(vertical="center")
            widths = [len(h) for h in t.headers]
            for row in t.rows:
                vals = [_cell(v) for v in row]
                ws.append(vals)
                for i, v in enumerate(vals):
                    if v is not None:
                        widths[i] = max(widths[i], min(len(str(v)), 70))
            if t.truncated:
                ws.append([f"truncated at {self.max_rows} rows; choose a shorter period"])
            for i, h in enumerate(t.headers, 1):
                letter = get_column_letter(i)
                ws.column_dimensions[letter].width = min(max(widths[i - 1] + 2, 10), 72)
                fmt = _number_format(h)
                if fmt:
                    for cell in ws[letter][1:]:
                        cell.number_format = fmt
            ws.freeze_panes = "A2"
            if t.rows:
                ws.auto_filter.ref = f"A1:{get_column_letter(len(t.headers))}{len(t.rows) + 1}"
        wb.properties.title = f"Elysium {KIND_FILE[kind]} {tr.start:%Y-%m-%d} - {tr.end:%Y-%m-%d}"
        wb.properties.creator = "elysium-wallet-tracker"
        wb.properties.created = datetime(2026, 1, 1)  # deterministic file metadata
        wb.properties.modified = datetime(2026, 1, 1)
        wb.save(path)


def _number_format(header: str) -> str | None:
    h = header.lower()
    if h.endswith("_utc") or "(utc" in h:
        return "yyyy-mm-dd hh:mm:ss"
    if h in ("return",) or h.endswith("_share"):
        return "0.00%"
    if h.endswith("_hype") and "price" in h:
        return "0.000000000000"
    if h.endswith("_hype") or h.startswith("agora_pnl"):
        return "#,##0.000000"
    return None
