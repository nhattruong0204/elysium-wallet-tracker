"""Truthful order status, receipts and positions (issue #94).

Everything here is a pure function of ledger rows (orders, legs, fills), so it can be tested
without a chain. Rules:

* a wallet-reported hash is "submitted", never "filled"; only a validated receipt moves it on;
* an approval confirming is NOT a trade: the label says a fresh quote is required;
* a reverted swap says "no tokens moved" and shows the gas actually paid;
* amounts come from `verified` fills only; an `unknown` fill makes the quantity and PnL unknown
  (never zero, never estimated); `rolled_back` fills are excluded, `provisional` ones are flagged;
* PnL uses the average-cost method with exact integer arithmetic (Fraction), rounded toward zero
  on output, and is `None` (unknown) when a sell has no verified basis. Gas is reported
  separately and is not netted into the trade PnL, so the number is reproducible from the fills."""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from typing import Any

LABELS = {
    "created": ("Draft", "Nothing has been sent to your wallet."),
    "quote_review": ("Reviewing quote", "Nothing has been sent to your wallet."),
    "requires_approval": ("Approval needed", "A finite token approval is needed before the swap."),
    "awaiting_wallet": ("Waiting for your wallet", "Confirm or reject the request in your wallet."),
    "submitted": ("Submitted — not filled", "Your wallet reported a transaction. Nothing is "
                  "confirmed until it is mined; no fill has been recorded."),
    "included": ("Included on chain — provisional", "Mined, but not yet final; it can still "
                 "be reorganized."),
    "reverted": ("Reverted on chain — no tokens moved", "The transaction failed on chain. Only gas was spent."),
    "finalized": ("Final", "Confirmed to the configured depth."),
    "orphaned": ("Block reorganized — re-checking", "The block left the canonical chain; the "
                 "outcome is being re-checked. Do not resend."),
    "ambiguous": ("Unconfirmed — checking the chain", "The result is not known yet. Do not resend; "
                  "check your wallet history."),
    "cancelled": ("Cancelled", "Nothing was sent."),
    "expired": ("Expired", "The quote or request expired. Nothing was sent."),
    "rejected": ("Rejected", "The wallet request was rejected. Nothing was sent."),
    "failed": ("Failed", "The order failed. See the reason."),
}


def _tx_link(links: Any | None, h: str | None) -> str | None:
    return links.tx(h) if (links is not None and h) else None


def leg_summary(leg: dict, links: Any | None = None) -> dict:
    tx = leg.get("final_tx_hash") or leg.get("tx_hash")
    return {"kind": leg["kind"], "state": leg["state"], "tx_hash": tx, "tx_url": _tx_link(links, tx),
            "block_number": leg.get("block_number"), "receipt_status": leg.get("receipt_status"),
            "gas_cost_wei": leg.get("gas_cost_wei"), "final": leg.get("finalized_at") is not None}


def order_status(order: dict, legs: list[dict], fills: list[dict],
                 links: Any | None = None) -> dict:
    """User-facing status for one order. `order` is OrderView.public(); inputs are not mutated."""
    state = order["state"]
    label, detail = LABELS.get(state, (state.replace("_", " ").title(), ""))
    swap_legs = [x for x in legs if x["kind"] == "swap"]
    approvals = [x for x in legs if x["kind"] in ("approve", "reset")]
    if state == "requires_approval" and any(a["state"] in ("included", "hash_reported") for a in approvals):
        done = [a for a in approvals if a["state"] == "included"]
        if done:
            label = "Approval confirmed — refresh quote required"
            detail = ("The approval is on chain. It is not a trade: nothing was bought or sold. "
                      "Request a fresh quote to continue.")
        else:
            label, detail = "Approval submitted — waiting for the chain", \
                "Your wallet reported the approval; it is not confirmed until it is mined."
    live = [f for f in fills if f["status"] != "rolled_back"]
    provisional = any(f["status"] == "provisional" for f in live) or (
        state in ("included", "reverted") and not live)
    gas = sum(int(f["gas_cost_wei"]) for f in live if f["verdict"] in ("verified", "unknown", "reverted"))
    out: dict[str, Any] = {
        "order_id": order["id"], "state": state, "label": label, "detail": detail,
        "provisional": provisional and state in ("included", "reverted"),
        "legs": [leg_summary(x, links) for x in legs],
        "gas_cost_wei": str(gas) if live else None,
        "fill": None,
    }
    swap_fill = next((f for f in reversed(live) if f["verdict"] in ("verified", "unknown")), None)
    if swap_fill is not None and swap_legs:
        out["fill"] = {"verdict": swap_fill["verdict"], "side": swap_fill["side"],
                       "native_wei": swap_fill["native_wei"], "token_wei": swap_fill["token_wei"],
                       "status": swap_fill["status"], "reason": swap_fill["reason"]}
        if swap_fill["verdict"] == "unknown":
            out["detail"] = ("Mined, but the exact amounts could not be verified from independent "
                             "on-chain evidence, so no fill or PnL is shown.")
    return out


def positions(fills: list[dict]) -> list[dict]:
    """Per (wallet, token) from fills of ANY order. `fills` as returned by OrderLedger.get_fills.

    quantity_wei / cost_basis_wei / realized_pnl_wei are decimal strings or None (= unknown)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in sorted(fills, key=lambda r: (r["block_number"], r["id"])):
        if f["status"] == "rolled_back" or f["verdict"] == "reverted":
            continue
        groups.setdefault((f["wallet"], f["token"]), []).append(f)

    rows = []
    for (wallet, token), items in groups.items():
        qty = 0
        cost = Fraction(0)
        realized = Fraction(0)
        known = True                  # quantity and basis are trustworthy
        pnl_known = True
        for f in items:
            if f["verdict"] != "verified":
                known = pnl_known = False
                continue
            t, n = int(f["token_wei"]), int(f["native_wei"])
            if f["side"] == "buy":
                qty += t
                cost += n
            else:
                if qty <= 0 or t > qty:
                    known = pnl_known = False       # sold something we never saw bought
                    continue
                part = cost * t / qty
                realized += n - part
                cost -= part
                qty -= t
        provisional = any(f["status"] == "provisional" for f in items)
        rows.append({
            "wallet": wallet, "token": token,
            "quantity_wei": str(qty) if known else None,
            "cost_basis_wei": str(int(cost)) if known else None,
            "realized_pnl_wei": str(int(realized)) if pnl_known else None,
            "complete": known, "provisional": provisional,
            "fills": len(items),
            "note": None if known else "unverified or missing fills: quantity and PnL are unknown",
        })
    return rows


def wallet_overview(ledger: Any, user_id: int, links: Any | None = None, *,
                    limit: int = 10) -> list[dict]:
    """Latest orders with truthful status (per-user via RLS)."""
    out = []
    for v in ledger.list_orders(user_id, limit=limit):
        o = v.public()
        out.append(order_status(o, ledger.get_legs(user_id, o["id"]),
                                ledger.get_fills(user_id, o["id"], include_rolled_back=True), links))
    return out


def render_text(items: list[dict], pos: list[dict], esc: Callable[[str], str]) -> str:
    """Telegram HTML: all dynamic values pass through `esc`."""
    lines: list[str] = []
    for it in items:
        lines.append(f"<b>{esc(it['label'])}</b> — <code>{esc(it['order_id'][:8])}</code>")
        lines.append(esc(it["detail"]))
        for lg in it["legs"]:
            if lg["tx_url"]:
                lines.append(f"{esc(lg['kind'])}: <a href=\"{esc(lg['tx_url'])}\">{esc(lg['tx_hash'][:10])}…</a>"
                             f" ({esc(lg['state'])}{', final' if lg['final'] else ''})")
        if it["fill"] and it["fill"]["verdict"] == "verified":
            f = it["fill"]
            lines.append(f"verified fill ({esc(f['status'])}): native {esc(f['native_wei'])} wei, "
                         f"tokens {esc(f['token_wei'])} wei")
        if it["gas_cost_wei"] is not None:
            lines.append(f"gas paid: {esc(it['gas_cost_wei'])} wei")
    for p in pos:
        q = p["quantity_wei"] if p["quantity_wei"] is not None else "unknown"
        pnl = p["realized_pnl_wei"] if p["realized_pnl_wei"] is not None else "unknown"
        lines.append(f"<code>{esc(p['token'])}</code>: held {esc(q)} wei, realized PnL {esc(pnl)} wei"
                     + (" (provisional)" if p["provisional"] else ""))
    return "\n".join(lines)
