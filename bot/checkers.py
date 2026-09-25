"""Deterministic checkers ("gateway controls"). They sit outside the AI layer and can block.

Four checks run on every proposal before anyone (human, policy or LLM) sees it:
  1. data integrity  - are the inputs fresh and sane?
  2. rule compliance - lots, ticks, ARA/ARB, suspension, cash cover, calendar (IDX rules)
  3. risk gate       - per-name / gross / order-size / order-count limits and the
                       ACTIVE -> REDUCING -> HALTED state machine (NautilusTrader-style)
  4. committee validator - the LLM committee's JSON is well-formed and can only veto/shrink

A fifth, the reconciler, compares what was proposed with what was filled.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import engine, portfolio

WIB = timezone(timedelta(hours=7))

# Risk-gate limits. All are fractions of account equity unless stated.
LIMITS = dict(
    max_name_w=0.22,        # 20% target cap + 2pp drift tolerance
    max_order_frac=0.25,    # one order may not exceed 25% of equity
    max_orders_day=12,      # buys + sells per session
    daily_loss_reducing=-0.04,   # equity down 4% vs previous close -> REDUCING (exits only)
    drawdown_halt=-0.15,         # equity 15% below its peak -> HALTED (nothing but cancels)
    ihsg_reducing=-0.05,         # IHSG down 5% intraday -> REDUCING
    ihsg_halt=-0.08,             # IHSG down 8% (exchange halt level) -> HALTED until next EOD
    stale_sessions=1,            # data older than this many sessions -> no new buys
    losing_streak=5,             # this many consecutive losing sessions -> REDUCING (cooldown, no new buys)
)


# ------------------------------------------------------------------ 1. data integrity
def check_data(latest: dict, now_wib: datetime | None = None) -> dict:
    """Freshness and sanity of the engine snapshot. Fail = no new buys (sells still allowed)."""
    now = now_wib or datetime.now(WIB)
    asof = datetime.strptime(latest["asof"], "%Y-%m-%d").replace(tzinfo=WIB)
    # expected: the last completed session on or before today (16:02 close); before 16:15 today counts as yesterday
    exp = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 16 or (now.hour == 16 and now.minute < 15):
        exp -= timedelta(days=1)
    while exp.weekday() >= 5 or exp.strftime("%Y-%m-%d") in engine.HOLIDAYS:
        exp -= timedelta(days=1)
    sessions_behind = 0
    d = exp
    while d.date() > asof.date():
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in engine.HOLIDAYS:
            sessions_behind += 1
        d -= timedelta(days=1)
    stocks = latest["stocks"]
    missing = [s["sym"] for s in stocks if s.get("close") is None]
    no_model = [s["sym"] for s in stocks if s.get("sigd") is None and s.get("close") is not None]
    big_moves = [s["sym"] for s in stocks if s.get("close") and s.get("prev_close")
                 and abs(s["close"] / s["prev_close"] - 1) > 0.36]
    notes = []
    if sessions_behind > LIMITS["stale_sessions"]:
        notes.append(f"prices are {sessions_behind} sessions old (expected {exp:%Y-%m-%d}, have {latest['asof']})")
    if missing:
        notes.append("no price for " + ", ".join(missing))
    if big_moves:
        notes.append("impossible daily move (>36%) for " + ", ".join(big_moves))
    if len(no_model) > 5:
        notes.append(f"{len(no_model)} names without a volatility forecast")
    return dict(ok=not notes, sessions_behind=sessions_behind, expected_asof=exp.strftime("%Y-%m-%d"),
                missing=missing, no_model=no_model, big_moves=big_moves, notes=notes)


# ------------------------------------------------------------------ 2. rule compliance
def tick_ok(price: float, session: str) -> bool:
    if session >= "2026-09-28" and price <= 10:
        return float(price).is_integer() and price >= 1
    t = portfolio.tick_size(price)
    return abs(price / t - round(price / t)) < 1e-6


def check_rules(order: dict, stock: dict, session: str) -> dict:
    """One order against the IDX rulebook. Returns ok + list of violated rules."""
    v = []
    if order["shares"] <= 0 or order["shares"] % 100:
        v.append("quantity must be whole 100-share lots")
    px = float(order["limit"])
    if not tick_ok(px, session):
        v.append(f"limit {px:,.0f} is not on a valid tick")
    ara, arb = portfolio.price_limits(float(stock["close"]), session)
    if px > ara + 1e-9 or px < arb - 1e-9:
        v.append(f"limit {px:,.0f} outside the day's price band {arb:,.0f}-{ara:,.0f}")
    if stock.get("status", "").startswith("Not traded"):
        v.append("stock did not trade in the last session (suspended?)")
    if order["side"] == "buy" and stock.get("ara_locked"):
        v.append("closed locked at ARA: buying would chase a limit-up")
    if session in engine.HOLIDAYS or datetime.strptime(session, "%Y-%m-%d").weekday() >= 5:
        v.append(f"{session} is not a trading day")
    if order["side"] == "buy" and stock.get("sigd") is None:
        v.append("no volatility forecast for this name")
    return dict(ok=not v, violations=v, ara=ara, arb=arb)


def check_cash(orders: list[dict], cash: float, P: dict) -> dict:
    """Aggregate cash cover: buys must be payable from cash plus the session's sells (T+2 both ways)."""
    inflow = sum(o["shares"] * o["limit"] * (1 - P["sell_fee"]) for o in orders if o["side"] == "sell")
    need = sum(o["shares"] * o["limit"] * (1 + P["buy_fee"]) for o in orders if o["side"] == "buy")
    ok = need <= cash + inflow + 1
    return dict(ok=ok, need=need, available=cash + inflow, note=None if ok else "buys exceed cash plus expected sell proceeds")


# ------------------------------------------------------------------ 3. risk gate
class RiskGate:
    """State machine persisted in docs/data/risk_state.json.
    ACTIVE   - all orders allowed within limits
    REDUCING - only orders that reduce a position (sells) are allowed
    HALTED   - nothing except cancels; needs an explicit resume (or the next EOD run for auto-halts)
    """

    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text()) if path.exists() else dict(
            state="ACTIVE", reason="", since=None, manual=False, peak_equity=None, orders_today={}, log=[])

    def save(self) -> None:
        self.state["log"] = self.state["log"][-100:]
        self.path.write_text(json.dumps(self.state, separators=(",", ":")))

    def set(self, state: str, reason: str, manual: bool = False) -> None:
        if self.state["state"] == state and self.state.get("reason") == reason:
            return
        self.state.update(state=state, reason=reason, since=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                          manual=manual)
        self.state["log"].append(dict(t=self.state["since"], state=state, reason=reason, manual=manual))
        self.save()

    def auto_release(self) -> None:
        """Called by the EOD job: automatic halts/reductions lapse at the next end of day; manual ones don't."""
        if self.state["state"] != "ACTIVE" and not self.state.get("manual"):
            self.set("ACTIVE", "automatic state released at end of day")

    def update_equity(self, equity: float, prev_equity: float | None) -> None:
        pk = self.state.get("peak_equity") or equity
        self.state["peak_equity"] = max(pk, equity)
        dd = equity / self.state["peak_equity"] - 1
        if prev_equity:
            self.state["streak"] = self.state.get("streak", 0) + 1 if equity < prev_equity else 0
        if self.state.get("streak", 0) >= LIMITS["losing_streak"] and self.state["state"] == "ACTIVE":
            self.set("REDUCING", f"{self.state['streak']} losing sessions in a row: cooldown, no new buys")
        if dd <= LIMITS["drawdown_halt"]:
            self.set("HALTED", f"drawdown {dd:.1%} from the equity peak (limit {LIMITS['drawdown_halt']:.0%})")
        elif prev_equity and equity / prev_equity - 1 <= LIMITS["daily_loss_reducing"] and self.state["state"] == "ACTIVE":
            self.set("REDUCING", f"daily loss {equity / prev_equity - 1:.1%} (limit {LIMITS['daily_loss_reducing']:.0%})")
        self.save()

    def update_ihsg(self, chg: float) -> None:
        if chg <= LIMITS["ihsg_halt"]:
            self.set("HALTED", f"IHSG {chg:.1%} intraday: exchange halt level")
        elif chg <= LIMITS["ihsg_reducing"] and self.state["state"] == "ACTIVE":
            self.set("REDUCING", f"IHSG {chg:.1%} intraday")

    def evaluate(self, orders: list[dict], stocks: dict, st: dict, session: str, data_ok: bool) -> list[dict]:
        """Per-order verdict: allow / downsize / block, with the limit that applied."""
        equity = st["cash"] + sum(p["shares"] * stocks[k]["close"] for k, p in st["positions"].items() if k in stocks)
        n_today = self.state.get("orders_today", {}).get(session, 0)
        out = []
        for o in orders:
            v = dict(verdict="allow", limit="", shares=o["shares"])
            notional = o["shares"] * o["limit"]
            cur = st["positions"].get(o["sym"], {}).get("shares", 0) * stocks[o["sym"]]["close"]
            reducing = o["side"] == "sell"
            if self.state["state"] == "HALTED":
                v.update(verdict="block", limit=f"risk gate HALTED: {self.state['reason']}")
            elif self.state["state"] == "REDUCING" and not reducing:
                v.update(verdict="block", limit=f"risk gate REDUCING: {self.state['reason']}")
            elif not data_ok and not reducing:
                v.update(verdict="block", limit="data integrity check failed: no new buys")
            elif n_today + len(out) >= LIMITS["max_orders_day"]:
                v.update(verdict="block", limit=f"more than {LIMITS['max_orders_day']} orders in one session")
            elif not reducing and notional > LIMITS["max_order_frac"] * equity:
                cap = math.floor(LIMITS["max_order_frac"] * equity / o["limit"] / 100) * 100
                v.update(verdict="downsize", shares=int(cap), limit=f"order capped at {LIMITS['max_order_frac']:.0%} of equity")
            elif not reducing and (cur + notional) > LIMITS["max_name_w"] * equity:
                cap = math.floor(max(0.0, LIMITS["max_name_w"] * equity - cur) / o["limit"] / 100) * 100
                v.update(verdict="downsize" if cap > 0 else "block", shares=int(cap),
                         limit=f"position capped at {LIMITS['max_name_w']:.0%} of equity")
            out.append(v)
        return out

    def count(self, session: str, n: int) -> None:
        self.state.setdefault("orders_today", {})[session] = self.state["orders_today"].get(session, 0) + n
        self.save()


# ------------------------------------------------------------------ 4. committee validator
VERDICTS = {"agree", "caution", "veto"}


def validate_committee(raw: dict | None, proposal: dict) -> dict:
    """The LLM committee may only agree, caution (size 0.5) or veto (size 0). Anything else is discarded."""
    if raw is None:
        return dict(verdict="unavailable", size=1.0, reason="committee not run", valid=False)
    try:
        verdict = str(raw.get("verdict", "")).lower().strip()
        if verdict not in VERDICTS:
            raise ValueError("bad verdict")
        size = {"agree": 1.0, "caution": 0.5, "veto": 0.0}[verdict]
        if proposal["side"] == "sell" and verdict == "veto":
            size, verdict = 1.0, "agree"  # the committee can never keep a risk-reducing exit from happening
        reason = str(raw.get("reason", ""))[:400]
        flags = [str(x)[:80] for x in (raw.get("red_flags") or [])][:6]
        if raw.get("ticker") and str(raw["ticker"]).upper() != proposal["sym"]:
            raise ValueError("ticker mismatch")
        return dict(verdict=verdict, size=size, reason=reason, red_flags=flags, valid=True,
                    model=raw.get("model"), tokens=raw.get("tokens"))
    except Exception as exc:
        return dict(verdict="invalid", size=1.0, reason=f"committee output rejected: {type(exc).__name__}", valid=False)


# ------------------------------------------------------------------ 5. reconciler
def reconcile(pending: list[dict], filled: list[dict], unfilled: list[dict], P: dict) -> dict:
    """Fill fidelity: which approved orders filled, at what slippage versus the limit model."""
    rows, tot_slip = [], []
    for t in filled:
        o = next((o for o in pending if o["sym"] == t["sym"] and o["side"] == t["side"]), None)
        if not o:
            continue
        ref = o.get("ref_close") or o["limit"]
        slip = (t["price"] / ref - 1) * (1 if t["side"] == "buy" else -1)
        tot_slip.append(slip)
        rows.append(dict(sym=t["sym"], side=t["side"], shares=t["shares"], price=t["price"], slippage=round(slip, 4)))
    return dict(filled=rows, unfilled=[dict(sym=o["sym"], side=o["side"], note=o.get("note")) for o in unfilled],
                avg_slippage=float(sum(tot_slip) / len(tot_slip)) if tot_slip else None,
                model_slippage=P["slippage"], fill_rate=len(rows) / len(pending) if pending else None)
