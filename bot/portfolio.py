"""Model portfolio: the bot runs its own paper account, so nothing is typed in by hand.

Every end of day it
  1. fills the orders it issued the evening before, at that session's prices,
     honouring the limit price (no fill if the price never reached it),
  2. values the holdings at the close and tracks each position's peak,
  3. issues the next session's orders from the rulebook in engine.py, and
  4. stores the stop and price-limit levels the intraday check watches.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine


def tick_size(p: float) -> int:
    return 1 if p < 200 else 2 if p < 500 else 5 if p < 2000 else 10 if p < 5000 else 25


def floor_tick(x: float) -> float:
    t = tick_size(x)
    return math.floor(x / t + 1e-9) * t


def ceil_tick(x: float) -> float:
    t = tick_size(x)
    return math.ceil(x / t - 1e-9) * t


def price_limits(ref: float, day: str) -> tuple[float, float]:
    """Upper (ARA) and lower (ARB) price limits for a session, from the previous close."""
    if day >= "2026-09-28" and ref <= 10:
        return ref + 1, max(1, ref - 1)
    reg = [r for r in engine.AR_REGIMES if r[0] <= day][-1]
    b = 0 if ref <= 200 else 1 if ref <= 5000 else 2
    floor_price = 1 if day >= "2026-09-28" else 50
    return floor_tick(ref * (1 + reg[1][b])), max(floor_price, ceil_tick(ref * (1 - reg[2][b])))


def new_state(day: str, cash: float) -> dict:
    return dict(version=1, start_date=day, start_cash=float(cash), cash=float(cash), realized=0.0,
                positions={}, pending=[], history=[], trades=[], last_processed=None)


def load(path: Path, day: str, cash: float) -> dict:
    return json.loads(path.read_text()) if path.exists() else new_state(day, cash)


def save(path: Path, st: dict) -> None:
    path.write_text(json.dumps(st, separators=(",", ":")))


def _bar(raw: pd.DataFrame, sym: str, day: str) -> dict | None:
    t, tk = pd.Timestamp(day), sym + ".JK"
    out = {}
    for f in ("Open", "High", "Low", "Close", "Volume"):
        try:
            v = raw[f][tk].get(t)
        except KeyError:
            return None
        if v is None or not np.isfinite(v):
            return None
        out[f] = float(v)
    return out if out["Volume"] > 0 else None


def make_order(side: str, s: dict, shares: int, why: str, session: str, P: dict) -> dict:
    ara, arb = price_limits(s["close"], session)
    if side == "buy":
        limit = min(ara, ceil_tick(s["close"] * (1 + P["buy_buffer"])))
    else:
        limit = max(arb, floor_tick(s["close"] * (1 - P["sell_buffer"])))
    return dict(side=side, sym=s["sym"], shares=int(shares), lots=int(shares) // 100, limit=float(limit),
                ara=float(ara), arb=float(arb), why=why, session=session)


def build_orders(latest: dict, st: dict) -> list[dict]:
    P, session = latest["params"], latest["next_session"]
    stocks = {s["sym"]: s for s in latest["stocks"]}
    equity = st["cash"] + sum(p["shares"] * stocks[k]["close"] for k, p in st["positions"].items() if k in stocks)
    orders, stopped = [], set()
    for sym, p in st["positions"].items():
        s = stocks.get(sym)
        if not s or p["shares"] <= 0:
            continue
        why = None
        if not s["sma200_ok"]:
            why = "Trend exit: closed below its 200-day average"
        elif s.get("stop_mult") and s["close"] < p["peak"] * s["stop_mult"]:
            why = f"Volatility stop: {s['close'] / p['peak'] - 1:.1%} from its peak of Rp{p['peak']:,.0f}"
        if why:
            stopped.add(sym)
            orders.append(make_order("sell", s, p["shares"], why, session, P))
    if latest["rebalance_today"]:
        for s in latest["stocks"]:
            sym = s["sym"]
            if sym in stopped:
                continue
            cur = st["positions"].get(sym, {}).get("shares", 0)
            tw = s.get("target_w") or 0.0
            cw = cur * s["close"] / equity if equity > 0 else 0.0
            if tw == 0 and cur == 0:
                continue
            if tw > 0 and cur > 0 and abs(tw - cw) < max(P["band"], P.get("resize_frac", 0.0) * tw):
                continue
            tgt = math.floor(tw * equity / s["close"] / 100) * 100 if tw > 0 else 0
            q = tgt - cur
            if q == 0 or (q > 0 and s.get("ara_locked")):
                continue
            if cur == 0:
                why = "New position: " + s["status"]
            elif tgt == 0:
                why = "Exit at rebalance: " + s["status"]
            else:
                why = f"Resize from {cw:.1%} to {tw:.1%} of the account"
            orders.append(make_order("buy" if q > 0 else "sell", s, abs(q), why, session, P))
    buys = [o for o in orders if o["side"] == "buy"]
    avail = st["cash"] + sum(o["shares"] * o["limit"] * (1 - P["sell_fee"]) for o in orders if o["side"] == "sell")
    need = sum(o["shares"] * o["limit"] * (1 + P["buy_fee"]) for o in buys)
    if need > avail and need > 0:
        k = max(0.0, avail / need)
        for o in buys:
            o["shares"] = int(math.floor(o["shares"] * k / 100) * 100)
            o["lots"] = o["shares"] // 100
    return [o for o in orders if o["shares"] > 0]


def process_day(st: dict, raw: pd.DataFrame, latest: dict) -> tuple[dict, dict, bool]:
    """Advance the model account by one session. Returns (state, events, is_new_day)."""
    P, day = latest["params"], latest["asof"]
    ev = dict(filled=[], unfilled=[])
    if st.get("last_processed") and day <= st["last_processed"]:
        return st, ev, False
    for o in st.get("pending", []):
        if o["session"] != day:
            continue
        bar = _bar(raw, o["sym"], day)
        if bar is None:
            ev["unfilled"].append(dict(o, note="no trading that day"))
            continue
        if o["side"] == "buy":
            if bar["Open"] <= o["limit"]:
                px = min(o["limit"], bar["Open"] * (1 + P["slippage"]))
            elif bar["Low"] <= o["limit"]:
                px = o["limit"]
            else:
                ev["unfilled"].append(dict(o, note="price stayed above the limit"))
                continue
            shares = min(o["shares"], math.floor(st["cash"] / (px * (1 + P["buy_fee"])) / 100) * 100)
            if shares <= 0:
                ev["unfilled"].append(dict(o, note="not enough cash"))
                continue
            cost = shares * px * (1 + P["buy_fee"])
            st["cash"] -= cost
            pos = st["positions"].setdefault(o["sym"], dict(shares=0, cost=0.0, entry=day, peak=bar["Close"]))
            if pos["shares"] == 0:
                pos.update(entry=day, peak=bar["Close"], cost=0.0)
            pos["shares"] += shares
            pos["cost"] += cost
        else:
            pos = st["positions"].get(o["sym"])
            if not pos:
                continue
            if bar["Open"] >= o["limit"]:
                px = max(o["limit"], bar["Open"] * (1 - P["slippage"]))
            elif bar["High"] >= o["limit"]:
                px = o["limit"]
            else:
                ev["unfilled"].append(dict(o, note="price stayed below the limit"))
                continue
            shares = min(o["shares"], pos["shares"])
            proceeds = shares * px * (1 - P["sell_fee"])
            avg = pos["cost"] / pos["shares"]
            st["realized"] += proceeds - avg * shares
            st["cash"] += proceeds
            pos["shares"] -= shares
            pos["cost"] -= avg * shares
            if pos["shares"] <= 0:
                del st["positions"][o["sym"]]
        trade = dict(date=day, sym=o["sym"], side=o["side"], shares=int(shares), price=round(px, 2), why=o["why"])
        st["trades"].append(trade)
        ev["filled"].append(trade)
    stocks = {s["sym"]: s for s in latest["stocks"]}
    value = 0.0
    for sym, pos in st["positions"].items():
        c = stocks[sym]["close"]
        pos["peak"] = max(pos["peak"], c)
        pos["last"] = c
        value += pos["shares"] * c
    equity = st["cash"] + value
    st["history"].append(dict(date=day, equity=round(equity), invested=round(value / equity, 4) if equity else 0.0))
    st["pending"] = build_orders(latest, st)
    for sym, pos in st["positions"].items():
        s = stocks[sym]
        pos["stop_price"] = round(pos["peak"] * s["stop_mult"], 2) if s.get("stop_mult") else None
        pos["ara_next"], pos["arb_next"] = price_limits(s["close"], latest["next_session"])
        pos["trend_ok"] = bool(s["sma200_ok"])
    st["last_processed"] = day
    st["trades"] = st["trades"][-500:]
    return st, ev, True
