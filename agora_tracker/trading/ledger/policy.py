"""Risk policy: versioned, immutable once stored, with hard ceilings that only a code change
(and review) can raise. All amounts are integer wei; nothing is a float."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields

HYPE = 10**18
ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")

# Ceilings: the owner may tighten freely, but cannot configure past these.
CEILINGS = {
    "per_trade_cap_wei": HYPE,                # the PAPER engine's 1 HYPE buy cap
    "daily_notional_cap_wei": 10 * HYPE,
    "global_daily_notional_cap_wei": 20 * HYPE,
    "daily_gas_cap_wei": HYPE // 2,
    "global_daily_gas_cap_wei": HYPE,
    "default_gas_reserve_wei": HYPE // 10,
    "max_slippage_bps": 100,                  # the plan engine rejects more than 1% anyway
    "max_price_impact_bps": 100,
    "max_active_orders_per_user": 5,
    "max_conflicting_orders_per_wallet": 5,
    "order_ttl_s": 900,
    "min_deadline_margin_s": 3600,
}
MAX_ALLOWLIST = 200


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RiskPolicy:
    per_trade_cap_wei: int = HYPE
    daily_notional_cap_wei: int = 3 * HYPE            # per user, per UTC day
    global_daily_notional_cap_wei: int = 5 * HYPE     # all users together
    daily_gas_cap_wei: int = HYPE // 20
    global_daily_gas_cap_wei: int = HYPE // 10
    default_gas_reserve_wei: int = HYPE // 100        # when a plan has no gas estimate
    max_slippage_bps: int = 100
    max_price_impact_bps: int = 100
    max_active_orders_per_user: int = 1
    max_conflicting_orders_per_wallet: int = 1
    order_ttl_s: int = 180
    min_deadline_margin_s: int = 60
    allowed_tokens: tuple[str, ...] = ()               # empty = nothing allowed (fail closed)

    def __post_init__(self) -> None:
        for name, ceiling in CEILINGS.items():
            v = getattr(self, name)
            if type(v) is not int or not 0 < v <= ceiling:
                raise PolicyError(f"{name} must be an integer in 1..{ceiling}")
        if self.daily_notional_cap_wei < self.per_trade_cap_wei:
            raise PolicyError("daily_notional_cap_wei cannot be below the per-trade cap")
        if self.global_daily_notional_cap_wei < self.daily_notional_cap_wei:
            raise PolicyError("the global daily cap cannot be below the per-user cap")
        if self.global_daily_gas_cap_wei < self.daily_gas_cap_wei:
            raise PolicyError("the global gas cap cannot be below the per-user cap")
        toks = self.allowed_tokens
        if (not isinstance(toks, tuple) or len(toks) > MAX_ALLOWLIST
                or any(not isinstance(t, str) or not ADDRESS.fullmatch(t) for t in toks)
                or list(toks) != sorted(set(toks))):
            raise PolicyError("allowed_tokens must be a sorted, unique tuple of lowercase "
                              f"addresses (at most {MAX_ALLOWLIST})")

    def params(self) -> dict:
        d = asdict(self)
        d["allowed_tokens"] = list(self.allowed_tokens)
        return d

    @classmethod
    def from_params(cls, params: dict) -> RiskPolicy:
        known = {f.name for f in fields(cls)}
        if not isinstance(params, dict) or set(params) - known:
            raise PolicyError("unknown policy fields")
        p = dict(params)
        p["allowed_tokens"] = tuple(p.get("allowed_tokens", ()))
        return cls(**p)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.params(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def with_allowed(cls, *tokens: str, **kw) -> RiskPolicy:
        return cls(allowed_tokens=tuple(sorted({t.lower() for t in tokens})), **kw)
