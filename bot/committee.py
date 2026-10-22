#!/usr/bin/env python3
"""AI committee: Claude reads the day's state and writes the short evening note. It never changes an order.

What it gets, in one compact brief: the regime and stress gauge, the positions with stops and P&L, tonight's orders
with limits and tomorrow's price-limit floor/cap, the candidates, the Kronos view, the news mood and known event dates.
What it returns, as JSON: a one-line market read, a plain reason plus agree/objection per order, a note per holding
without an order, and one line on what to watch. Written to docs/data/committee.json, appended to
docs/data/committee_log.csv (so its judgement can be scored later), and used by the Telegram summary.

Needs ANTHROPIC_API_KEY. Without it, or on any error, the summary falls back to the rulebook's own reasons.
Model: COMMITTEE_MODEL (default claude-sonnet-5-5); if that model is rejected the Haiku model used for news is tried.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine, news

MODEL = os.environ.get("COMMITTEE_MODEL") or "claude-sonnet-5-5"
FALLBACK_MODEL = news.MODEL
SYSTEM = (
    "You are the investment committee (analyst, risk manager and skeptic in one) for a rules-based Indonesian equity "
    "bot that trades liquid IDX large caps on daily data. The rulebook has already decided tonight's orders; you do not "
    "change them. Your job: explain each order in plain words a non-finance reader understands, say whether you would do "
    "the same (agree) and if not give a one-line objection, add a short note for each holding with no order, give a one-"
    "line read of the market day, and name the one thing that would change the picture tomorrow. Be concrete: use the "
    "rupiah levels in the brief. Treat the Kronos forecasts as weak evidence (16 samples, unreliable right after crash "
    "days) and the news mood as background. Never recommend averaging down into a falling position. "
    "Return the note by calling the committee_note tool. Lengths: market max 40 words, why max 14 words, objection "
    "max 18 words (empty when agree), note max 12 words, watch max 22 words."
)
# A forced tool call makes the API return the answer as parsed JSON, so a stray quote in the text cannot break it.
TOOL = {
    "name": "committee_note",
    "description": "The committee's evening note on tonight's orders and holdings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "market": {"type": "string"},
            "orders": {"type": "array", "items": {"type": "object", "properties": {
                "sym": {"type": "string"}, "why": {"type": "string"}, "agree": {"type": "boolean"},
                "objection": {"type": "string"}}, "required": ["sym", "why", "agree", "objection"]}},
            "holds": {"type": "array", "items": {"type": "object", "properties": {
                "sym": {"type": "string"}, "note": {"type": "string"}}, "required": ["sym", "note"]}},
            "watch": {"type": "string"},
        },
        "required": ["market", "orders", "holds", "watch"],
    },
}


def _f(x, nd=4):
    try:
        v = float(x)
        return None if not np.isfinite(v) else round(v, nd)
    except (TypeError, ValueError):
        return None


def brief(latest: dict, st: dict, kr: dict | None, summ: dict) -> dict:
    g, sv = latest["regime"], latest.get("svi") or {}
    stocks = {s["sym"]: s for s in latest["stocks"]}
    hist = st["history"][-1] if st.get("history") else {}
    eq = hist.get("equity") or st.get("cash", 0.0)
    drivers = sorted([c for c in sv.get("components", []) if c.get("z") is not None], key=lambda c: -c["z"])[:3]
    pos = []
    for sym, p in st["positions"].items():
        s = stocks.get(sym, {})
        last = s.get("close") or p.get("last") or 0.0
        avg = p["cost"] / p["shares"] if p.get("shares") else None
        pos.append(dict(sym=sym, lots=round(p["shares"] / 100, 1), avg=_f(avg, 0), last=_f(last, 0),
                        pnl_pct=_f(last / avg - 1, 3) if avg and last else None, stop=_f(p.get("stop_price"), 0),
                        trend=("above" if s.get("sma200_ok") else "below") + " 200-day" if s else "not in universe",
                        status=s.get("status"), managed=bool(p.get("managed", True))))
    orders = [dict(side=o["side"], sym=o["sym"], lots=o["lots"], limit=_f(o["limit"], 0),
                   floor_or_cap=_f(o.get("arb") if o["side"] == "sell" else o.get("ara"), 0), rule=o.get("why"))
              for o in st.get("pending", [])]
    cands = [dict(sym=s["sym"], rank=s.get("rank"), target_pct=_f(s.get("target_w", 0) * 100, 1), status=s.get("status"))
             for s in latest["stocks"] if s.get("selected")]
    names = {"IHSG"} | set(st["positions"]) | {c["sym"] for c in cands}
    kron = []
    if kr and kr.get("ok"):
        kron = [dict(sym=r["sym"], p_up5=_f(r["p_up5"], 2), median5=_f(r["ret5_med"], 3),
                     range5=[_f(r["ret5_p10"], 3), _f(r["ret5_p90"], 3)]) for r in kr["names"] if r["sym"] in names]
    mood = []
    for t in sorted(names - {"IHSG"}) + ["MARKET"]:
        d = summ.get(t)
        if d:
            pick = d["worst"] if abs(d["worst"]["s"]) >= abs(d["best"]["s"]) else d["best"]
            mood.append(dict(sym="IHSG" if t == "MARKET" else t, mood=_f(d["avg"], 2), n=d["n"], headline=pick["title"][:120]))
    today = pd.Timestamp(latest["asof"])
    events = [d for d in engine.EVENTS if 0 <= np.busday_count(today.date(), pd.Timestamp(d).date()) <= 15]
    return dict(
        date=latest["asof"], next_session=latest["next_session"], rebalance_day=bool(latest.get("rebalance_today")),
        market=dict(ihsg=_f(g["ihsg_close"], 0), ihsg_change=_f(g["ihsg_change"], 4),
                    ihsg_vs_200d="above" if g.get("ihsg_above") else "below",
                    brake="off (up to 100%)" if g["above"] else "on (at most 50% invested)",
                    stress_gauge=dict(value=_f(sv.get("value"), 2), tier=sv.get("label"),
                                      drivers=[dict(name=c["label"], z=_f(c["z"], 2)) for c in drivers]),
                    lower_price_limit=f"{latest['rules'].get('arb_pct', 0):.0%} per day (censored regime)" if latest.get("rules", {}).get("censored_regime") else "normal",
                    pct_universe_locked_today=_f(g.get("pct_locked_today"), 2)),
        account=dict(equity=_f(eq, 0), cash=_f(st.get("cash"), 0), invested_pct=_f(hist.get("invested", 0) * 100, 0),
                     since_start_pct=_f((eq / st["start_cash"] - 1) * 100, 1) if st.get("start_cash") else None),
        positions=pos, orders=orders, candidates=cands, kronos=kron, news=mood, msci_event_dates=events,
        notes=["Orders come from the rulebook: trend exits below the 200-day average, trailing volatility stops, "
               "weekly rebalance into the top risk-adjusted momentum names, inverse-vol weights, 15% vol target.",
               "Paper account only; the user mirrors orders by hand in his broker app."],
    )


def ask(b: dict, key: str, model: str = MODEL) -> dict | None:
    import requests

    for m in (model, FALLBACK_MODEL):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": m, "max_tokens": 2000, "system": SYSTEM, "tools": [TOOL],
                      "tool_choice": {"type": "tool", "name": TOOL["name"]},
                      "messages": [{"role": "user", "content": json.dumps(b, ensure_ascii=False)}]})
            if r.status_code in (400, 404) and "model" in r.text.lower() and m != FALLBACK_MODEL:
                print(f"Committee: model {m} rejected, trying {FALLBACK_MODEL}")
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("stop_reason") == "max_tokens":
                raise ValueError("reply cut off at max_tokens")
            out = next((x["input"] for x in body.get("content", []) if x.get("type") == "tool_use"), None)
            if out is None:  # no tool call: fall back to reading JSON out of the text
                text = "".join(x.get("text", "") for x in body.get("content", []))
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                out = json.loads(text[text.find("{"): text.rfind("}") + 1])
            out = dict(market=str(out.get("market", ""))[:300], watch=str(out.get("watch", ""))[:200],
                       orders=[dict(sym=str(o.get("sym", "")).upper(), why=str(o.get("why", ""))[:120],
                                    agree=bool(o.get("agree", True)), objection=str(o.get("objection", ""))[:160])
                               for o in out.get("orders", [])],
                       holds=[dict(sym=str(h.get("sym", "")).upper(), note=str(h.get("note", ""))[:120])
                              for h in out.get("holds", [])], model=m)
            return out
        except Exception as e:
            print("Committee: call failed:", type(e).__name__, str(e)[:160])
            if m == FALLBACK_MODEL:
                return None
    return None


def rules_only(latest: dict, st: dict) -> dict:
    g = latest["regime"]
    return dict(model="rules", market=f"IHSG {g['ihsg_close']:,.0f} ({g['ihsg_change']:+.1%}); the rulebook's reasons are shown "
                "because no AI committee key is set.", watch="",
                orders=[dict(sym=o["sym"], why=o.get("why", ""), agree=True, objection="") for o in st.get("pending", [])],
                holds=[])


def run(latest: dict, st: dict, kr: dict | None, summ: dict, key: str | None, outdir: Path) -> dict:
    b = brief(latest, st, kr, summ)
    out = ask(b, key) if key else None
    source = out["model"] if out else "rules"
    if out is None:
        out = rules_only(latest, st)
    # keep every order the rulebook issued, even if the model skipped one
    byname = {o["sym"]: o for o in out["orders"]}
    out["orders"] = [dict(sym=o["sym"], side=o["side"], lots=o["lots"], limit=o["limit"],
                          floor_or_cap=o.get("arb") if o["side"] == "sell" else o.get("ara"), rule=o.get("why"),
                          why=byname.get(o["sym"], {}).get("why") or o.get("why", ""),
                          agree=byname.get(o["sym"], {}).get("agree", True),
                          objection=byname.get(o["sym"], {}).get("objection", "")) for o in st.get("pending", [])]
    out.update(date=latest["asof"], next_session=latest["next_session"], source=source,
               generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    (outdir / "committee.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    log = outdir / "committee_log.csv"
    rows = [dict(date=out["date"], session=out["next_session"], sym=o["sym"], side=o["side"], lots=o["lots"], limit=o["limit"],
                 agree=o["agree"], objection=o["objection"], why=o["why"], source=source) for o in out["orders"]]
    if rows:
        old = pd.read_csv(log) if log.exists() else pd.DataFrame()
        new = pd.DataFrame(rows)
        if len(old):
            old = old[~((old["date"] == out["date"]))]
        pd.concat([old, new], ignore_index=True).to_csv(log, index=False)
    return out
