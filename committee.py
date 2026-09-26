"""The committee: LLM agents that review each proposal. Advisory only.

Authority rule (enforced in checkers.validate_committee): the committee can *agree*, *caution*
(halve the size) or *veto* a buy. It can never enlarge an order, add one, or stop a sell.

Backends (env COMMITTEE):
  lite          (default) one structured Claude call per proposal that plays the analyst,
                bull, bear and risk roles in turn and ends with a verdict. Cheap (~2-4k tokens).
  tradingagents runs TauricResearch/TradingAgents (pip install tradingagents) on the .JK ticker
                with the current book as PortfolioContext and maps its rating to a verdict.
  off           committee skipped; proposals carry verdict "unavailable".

Every call is logged (model, tokens) so the veto record can be scored later against what the
engine alone would have done.
"""
from __future__ import annotations

import json
import os
import re

import requests

DEFAULT_MODEL = "claude-sonnet-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"

ROLE_PROMPT = """You are a review committee for a rules-based Indonesian equity (IDX) trading system.
A deterministic model has produced the proposal below. Your job is NOT to generate ideas or to
size trades up. You may only: agree, raise a caution (size will be halved), or veto a buy.
Reasons must come from the supplied facts; if a fact is missing, say so instead of guessing.

Work through four roles in order, briefly:
1. Analyst: what do the price/trend/volatility/liquidity facts and the news say?
2. Bull: the strongest fact-based case for the proposal.
3. Bear: the strongest fact-based case against it (suspension, rights issue, regulator action,
   fraud/legal, earnings shock, liquidity, limit-lock risk, data anomaly).
4. Risk: given the portfolio context, does the size and timing make sense?

Then reply with ONLY a JSON object:
{"ticker": "...", "verdict": "agree" | "caution" | "veto",
 "red_flags": ["short fact-based flags"], "bull": "1 sentence", "bear": "1 sentence",
 "reason": "1-2 sentences naming the facts that decided the verdict"}
Veto only for hard red flags (suspension, UMA/special monitoring, fraud/regulatory action, rights
issue/dilution announced, data clearly wrong). Caution for soft concerns. Otherwise agree."""


def _context(p: dict, stock: dict, latest: dict, news: list[dict], st: dict) -> str:
    g = latest["regime"]
    held = {k: v["shares"] for k, v in st.get("positions", {}).items()}
    items = [n for n in news if p["sym"] in n.get("tickers", [])][:12]
    return json.dumps(dict(
        proposal=dict(side=p["side"], ticker=p["sym"], lots=p["lots"], limit=p["limit"], why=p["why"],
                      session=p["session"], target_weight=p.get("target_w"), rank=p.get("rank")),
        stock=dict(close=stock.get("close"), prev_close=stock.get("prev_close"), above_200d=stock.get("sma200_ok"),
                   return_6m=stock.get("mom126"), forecast_vol_annual=stock.get("siga"),
                   traded_value_bn_rp=stock.get("liq_bn"), status=stock.get("status"),
                   ara_locked=stock.get("ara_locked"), arb_locked=stock.get("arb_locked")),
        market=dict(ihsg=g.get("ihsg_close"), ihsg_change=g.get("ihsg_change"), ihsg_above_200d=g.get("above"),
                    exposure_cap=g.get("cap"), ihsg_forecast_vol=g.get("ihsg_vol_ann")),
        portfolio=dict(cash=st.get("cash"), positions=held, gross_target=latest["portfolio"].get("gross")),
        news_last_days=[dict(t=n.get("published", "")[:16], title=n["title"], score=n.get("s"), src=n.get("source"))
                        for n in items],
    ), ensure_ascii=False, default=str)


def _claude(prompt: str, key: str, model: str) -> tuple[dict | None, dict]:
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code == 404 and model != FALLBACK_MODEL:
        return _claude(prompt, key, FALLBACK_MODEL)
    r.raise_for_status()
    body = r.json()
    text = "".join(b.get("text", "") for b in body.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    out = json.loads(m.group(0)) if m else None
    usage = body.get("usage", {})
    return out, dict(model=body.get("model", model), tokens=(usage.get("input_tokens", 0) + usage.get("output_tokens", 0)))


def review_lite(p: dict, stock: dict, latest: dict, news: list[dict], st: dict, key: str) -> dict | None:
    model = os.environ.get("COMMITTEE_MODEL") or DEFAULT_MODEL
    prompt = ROLE_PROMPT + "\n\nFACTS (JSON):\n" + _context(p, stock, latest, news, st)
    try:
        out, meta = _claude(prompt, key, model)
        if out is None:
            return None
        out.update(meta)
        return out
    except Exception as exc:
        print("Committee (lite) failed:", type(exc).__name__, str(exc)[:120])
        return None


def review_tradingagents(p: dict, latest: dict, st: dict) -> dict | None:
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.portfolio import PortfolioContext
    except Exception as exc:
        print("TradingAgents not installed:", type(exc).__name__)
        return None
    try:
        cfg = DEFAULT_CONFIG.copy()
        cfg["llm_provider"] = os.environ.get("TA_PROVIDER", "anthropic")
        cfg["max_debate_rounds"] = int(os.environ.get("TA_DEBATE_ROUNDS", "1"))
        book = PortfolioContext.model_validate(dict(
            cash=float(st.get("cash", 0)), currency="IDR",
            positions=[dict(ticker=k + ".JK", quantity=v["shares"], average_price=v.get("cost", 0) / max(v["shares"], 1))
                       for k, v in st.get("positions", {}).items()]))
        _, decision = TradingAgentsGraph(debug=False, config=cfg).propagate(p["sym"] + ".JK", latest["asof"], portfolio=book)
        text = json.dumps(decision, default=str) if not isinstance(decision, str) else decision
        low = text.lower()
        if p["side"] == "buy":
            verdict = "veto" if "sell" in low else "caution" if "hold" in low else "agree"
        else:
            verdict = "agree"
        return dict(ticker=p["sym"], verdict=verdict, reason="TradingAgents rating: " + text[:200], red_flags=[],
                    model="tradingagents", tokens=None)
    except Exception as exc:
        print("TradingAgents run failed:", type(exc).__name__, str(exc)[:120])
        return None


def review_all(props: list[dict], latest: dict, news: list[dict], st: dict) -> list[dict | None]:
    backend = (os.environ.get("COMMITTEE") or "lite").lower()
    key = os.environ.get("ANTHROPIC_API_KEY")
    stocks = {s["sym"]: s for s in latest["stocks"]}
    out: list[dict | None] = []
    for p in props:
        if backend == "off" or (backend == "lite" and not key):
            out.append(None)
        elif backend == "tradingagents":
            out.append(review_tradingagents(p, latest, st))
        else:
            out.append(review_lite(p, stocks[p["sym"]], latest, news, st, key))
    return out
