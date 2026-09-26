"""Scheduled jobs for the IDX Trend Bot (run by GitHub Actions).

  python -m bot.tasks eod        after the close (17:30 WIB, retry 19:00)
  python -m bot.tasks intraday   every 30 minutes while the market is open
  python -m bot.tasks backtest   once a month
Options: --dry-run prints Telegram messages instead of sending them,
--cache FILE reads daily prices from a pickle (offline tests), --force ignores the clock.

Secrets (GitHub > Settings > Secrets and variables > Actions):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   where alerts go
  ANTHROPIC_API_KEY                  optional: better news sentiment
Variable: START_CASH (optional, default 100000000), the model account's size.
"""
from __future__ import annotations

import argparse
import html
import subprocess
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import approval, brokers, checkers, committee, engine, news, portfolio

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
WIB = timezone(timedelta(hours=7))


def rp(x: float) -> str:
    return f"Rp{x:,.0f}"


def day_label(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %d %b")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def send(text: str, dry: bool) -> None:
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not token or not chat:
        print("---- Telegram message (not sent) ----\n" + text + "\n-------------------------------------")
        return
    import requests

    for i in range(0, len(text), 3800):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json={
            "chat_id": chat, "text": text[i: i + 3800], "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code != 200:
            print("Telegram error", r.status_code, r.text[:300])


def dashboard_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return os.environ.get("DASHBOARD_URL", "")


def set_status(**kw) -> None:
    p = DATA / "status.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    st.update(kw)
    p.write_text(json.dumps(st, indent=1))


def load_raw(a) -> pd.DataFrame:
    return pd.read_pickle(a.cache) if a.cache else engine.download()


# ------------------------------------------------------------------ end of day


def eod_message(latest: dict, st: dict, ev: dict, summ: dict, store: dict) -> str:
    g, hist = latest["regime"], st["history"]
    eq, start = hist[-1]["equity"], st["start_cash"]
    out = [f"<b>IDX Trend Bot: {day_label(latest['asof'])} close</b>",
           f"IHSG {g['ihsg_close']:,.0f} ({g['ihsg_change']:+.2%}). " +
           ("Market brake off." if g["above"] else "Market brake on: at most 50% invested."),
           f"Model account {rp(eq)} ({eq / start - 1:+.2%} since {day_label(st['start_date'])}), "
           f"{hist[-1]['invested']:.0%} invested. Paper money only."]
    sh = store.get("shadow")
    if sh and sh.get("equity"):
        out.append(f"Shadow account (pure strategy, every proposal taken): {rp(sh['equity'])} "
                   f"({sh['equity'] / (sh.get('start_cash') or start) - 1:+.2%} since its start).")
    if ev["filled"]:
        out += ["", "<b>Filled today</b>"] + [
            f"• {'Bought' if t['side'] == 'buy' else 'Sold'} {t['sym']} {t['shares'] // 100} lots at {rp(t['price'])}"
            for t in ev["filled"]]
    if ev["unfilled"]:
        out += ["", "<b>Not filled</b>"] + [f"• {o['side'].title()} {o['sym']}: {esc(o['note'])}" for o in ev["unfilled"]]
    out += ["", f"<b>Proposals for {day_label(latest['next_session'])}</b> (mode: {store.get('mode')}, risk gate {store.get('gate')})"]
    if store.get("items"):
        for p in store["items"]:
            c = p["committee"].get("verdict", "")
            tag = {"approved": "approved", "awaiting": "WAITING FOR YOUR TAP", "blocked": "blocked", "rejected": "rejected",
                   "vetoed": "vetoed by committee"}.get(p["status"], p["status"])
            out.append(f"• {p['side'].title()} {p['sym']} {max(p['final_shares'], p['shares']) // 100} lots, limit {rp(p['limit'])} — {tag}"
                       + (f", committee: {c}" if c and c not in ("unavailable", "invalid") else ""))
            if p["blocked"]:
                out.append("   ↳ " + esc("; ".join(p["blocked"])))
    elif latest["rebalance_today"]:
        out.append("None: the holdings already match the targets.")
    else:
        out.append(f"None. Next rebalance after the {day_label(latest['next_rebalance_close'])} close.")
    if not store.get("data", {}).get("ok", True):
        out.append("Data check failed: " + esc("; ".join(store["data"]["notes"])) + ". No new buys.")
    lines = []
    for t in list(st["positions"]) + ["MARKET"]:
        d = summ.get(t)
        if not d:
            continue
        pick = d["worst"] if abs(d["worst"]["s"]) >= abs(d["best"]["s"]) else d["best"]
        lines.append(f"• {'Market' if t == 'MARKET' else t} {d['avg']:+.2f} ({d['n']} headlines): {esc(pick['title'][:110])}")
    if lines:
        out += ["", "<b>News mood, last 24h</b> (−1 to +1)"] + lines
    url = dashboard_url()
    if url:
        out += ["", f'<a href="{url}">Open the dashboard</a>']
    return "\n".join(out)


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in engine.HOLIDAYS:
        return False
    hm = now.hour * 60 + now.minute
    return 8 * 60 + 45 <= hm <= 16 * 60 + 15


def quotes(syms: list[str]) -> dict:
    """Latest 5-minute prices from Yahoo Finance (may lag the exchange by several minutes)."""
    import yfinance as yf

    tks = [s + ".JK" for s in syms] + [engine.INDEX]
    df = yf.download(tks, period="1d", interval="5m", progress=False, auto_adjust=False,
                     group_by="ticker", threads=True)
    out = {}
    if df is None or df.empty:
        return out
    for s, tk in zip(syms + ["IHSG"], tks):
        try:
            if isinstance(df.columns, pd.MultiIndex):
                c = df[tk]["Close"] if tk in df.columns.get_level_values(0) else df["Close"][tk]
            else:
                c = df["Close"]
        except KeyError:
            continue
        c = c.dropna()
        if len(c):
            ts = c.index[-1]
            ts = ts.tz_convert("Asia/Jakarta") if ts.tzinfo else ts
            out[s] = (float(c.iloc[-1]), ts)
    return out



# ------------------------------------------------------------------ shared


def start_cash() -> float:
    return float(os.environ.get("START_CASH") or 100_000_000)


def use_slip() -> bool:
    return (os.environ.get("BROKER") or "paper").lower() == "slip"


def slip_broker() -> brokers.SlipBroker:
    return brokers.SlipBroker(DATA / "slip.json", DATA / "real_account.json", start_cash())


def load_store(name: str = "proposals.json") -> dict:
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else {}


def save_store(store: dict, name: str = "proposals.json") -> None:
    (DATA / name).write_text(json.dumps(store, separators=(",", ":"), default=str))


# ------------------------------------------------------------------ end of day


def cmd_eod(a) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    raw = load_raw(a)
    ppath = DATA / "portfolio.json"
    prev = portfolio.load(ppath, "1970-01-01", start_cash())
    holdings = [k for k, v in prev["positions"].items() if v["shares"] > 0]
    engine.configure(v2=(os.environ.get("RULES") or "v2").lower() == "v2", broker=os.environ.get("BROKER_FEES") or None)
    engine.run(raw, False, DATA, holdings)
    latest = json.loads((DATA / "latest.json").read_text())
    st = portfolio.load(ppath, latest["asof"], start_cash())
    approved_before = list(st.get("pending", []))
    st, ev, new_day = portfolio.process_day(st, raw, latest)
    force = a.force or (os.environ.get("FORCE_SEND") or "").lower() in ("1", "true", "yes")
    if not new_day:
        # Same session as the last run (weekend, holiday, prices not out yet, or a manual re-run):
        # keep the existing proposals untouched, refresh news/status, and only send if forced.
        portfolio.save(ppath, st)
        store = load_store()
        key = os.environ.get("ANTHROPIC_API_KEY") or None
        items, _ = news.update(DATA / "news.json", engine.UNIVERSE, key)
        summ = news.summary(items, 24)
        set_status(last_eod_utc=now_utc(), eod_asof=latest["asof"], approval_mode=approval.mode())
        print(f"No new session since the last run (prices to {latest['asof']}, already processed). Proposals kept.")
        if force and store:
            send(eod_message(latest, st, ev, summ, store), a.dry_run)
            if store.get("session", "") >= datetime.now(WIB).strftime("%Y-%m-%d"):
                approval.send_proposals([p for p in store.get("items", []) if p["status"] == "awaiting"], rp, esc, a.dry_run)
        elif force:
            send(eod_message(latest, st, ev, summ, dict(mode=approval.mode(), gate="ACTIVE", items=[])), a.dry_run)
        return
    candidates, st["pending"] = st["pending"], []      # candidate orders are proposals, not orders
    portfolio.save(ppath, st)
    # Shadow account: follows every proposal automatically = the pure strategy record, so that manual
    # approvals (or missed taps) never blur what the rules alone would have done.
    shpath = DATA / "portfolio_shadow.json"
    shst = portfolio.load(shpath, latest["asof"], start_cash())
    shst, _, _ = portfolio.process_day(shst, raw, latest)
    portfolio.save(shpath, shst)
    session = latest["next_session"]
    gate = checkers.RiskGate(DATA / "risk_state.json")
    if new_day:
        gate.auto_release()
    hist = st["history"]
    gate.update_equity(hist[-1]["equity"], hist[-2]["equity"] if len(hist) > 1 else None)
    key = os.environ.get("ANTHROPIC_API_KEY") or None
    items, _ = news.update(DATA / "news.json", engine.UNIVERSE, key)
    summ = news.summary(items, 24)
    if new_day:
        news.log_daily(DATA / "sentiment_log.csv", latest["asof"], summ)
    # --- checkers (deterministic)
    data_check = checkers.check_data(latest)
    stocks = {s["sym"]: s for s in latest["stocks"]}
    rules = [checkers.check_rules(o, stocks[o["sym"]], session) for o in candidates]
    risk = gate.evaluate(candidates, stocks, st, session, data_check["ok"])
    cash_check = checkers.check_cash(candidates, st["cash"], latest["params"])
    none_c = [dict(verdict="unavailable", size=1.0, reason="", valid=False)] * len(candidates)
    props = approval.build_proposals(candidates, latest, rules, risk, none_c, cash_check, data_check)
    # --- committee (LLM, advisory) on the proposals that survived the checkers
    live = [p for p in props if not p["blocked"]]
    raw_c = committee.review_all(live, latest, items, st) if live else []
    co_by_id = {p["id"]: checkers.validate_committee(r, p) for p, r in zip(live, raw_c)}
    co_list = [co_by_id.get(p["id"], none_c[0]) for p in props]
    props = approval.build_proposals(candidates, latest, rules, risk, co_list, cash_check, data_check)
    m = approval.mode()
    approval.decide_immediately(props, m, gate.state["state"])
    approval.apply_external([dict(items=props)], DATA / "approvals.json")
    store = dict(session=session, asof=latest["asof"], created_utc=now_utc(), mode=m, gate=gate.state["state"],
                 data=data_check, cash=cash_check, items=props,
                 shadow=dict(equity=shst["history"][-1]["equity"] if shst["history"] else None, start_cash=shst["start_cash"],
                             invested=shst["history"][-1]["invested"] if shst["history"] else None,
                             pending=len(shst["pending"])))
    orders = approval.approved_orders(store)      # empty in manual mode until the pre-open job
    st["pending"] = orders
    portfolio.save(ppath, st)
    if use_slip() and orders:
        slip_broker().submit(orders)
    if orders:
        gate.count(session, len(orders))
    save_store(store)
    n_logged = approval.log_decisions(DATA / "decisions.jsonl", store, dict(rules=latest["params"].get("hold_rank") and "v2" or "v1"))
    recon = checkers.reconcile(approved_before, ev["filled"], ev["unfilled"], latest["params"])
    (DATA / "reconcile.json").write_text(json.dumps(dict(asof=latest["asof"], **recon), separators=(",", ":"), default=str))
    set_status(last_eod_utc=now_utc(), eod_asof=latest["asof"], sentiment_method="claude" if key else "keywords",
               approval_mode=m, risk_gate=gate.state["state"], rules="v2" if latest["params"].get("hold_rank") else "v1",
               committee=(os.environ.get("COMMITTEE") or "lite") if key or os.environ.get("COMMITTEE") == "tradingagents" else "off",
               decisions_logged=n_logged)
    print(f"proposals: {len(props)} ({sum(p['status'] == 'approved' for p in props)} approved, "
          f"{sum(p['status'] == 'awaiting' for p in props)} awaiting, {sum(p['status'] == 'blocked' for p in props)} blocked, "
          f"{sum(p['status'] == 'vetoed' for p in props)} vetoed); gate {gate.state['state']}; mode {m}")
    send(eod_message(latest, st, ev, summ, store), a.dry_run)
    approval.send_proposals([p for p in props if p["status"] == "awaiting"], rp, esc, a.dry_run)


# ------------------------------------------------------------------ pre-open: collect taps, finalise orders


def cmd_preopen(a) -> None:
    store = load_store()
    if not store:
        print("No proposals yet; run the end-of-day job first.")
        return
    gate = checkers.RiskGate(DATA / "risk_state.json")
    fills: list[dict] = []
    events = approval.collect(store, DATA / "telegram_offset.json", gate, fills)
    intra = load_store("proposals_intraday.json")
    if intra:
        events += approval.collect(intra, DATA / "telegram_offset.json", gate, fills)
    events += approval.apply_external([store] + ([intra] if intra else []), DATA / "approvals.json")
    if intra:
        save_store(intra, "proposals_intraday.json")
    approval.expire(store, store.get("mode", approval.mode()))
    store["gate"] = gate.state["state"]
    orders = approval.approved_orders(store)
    if gate.state["state"] == "HALTED":
        orders = []
        events.append("risk gate HALTED: no orders go out")
    ppath = DATA / "portfolio.json"
    st = portfolio.load(ppath, store["asof"], start_cash())
    st["pending"] = orders
    portfolio.save(ppath, st)
    if use_slip():
        sb = slip_broker()
        if fills:
            fees = (engine.FEES.get(os.environ.get("BROKER_FEES") or "ipot"))
            done = sb.record_fills(fills, fees)
            events += [f"real fill: {d['side']} {d['sym']} {d['shares'] // 100} lots at {rp(d['price'])}" for d in done]
        if orders:
            sb.submit(orders)
    save_store(store)
    n = approval.log_decisions(DATA / "decisions.jsonl", store)
    set_status(last_preopen_utc=now_utc(), risk_gate=gate.state["state"], decisions_logged=n)
    out = [f"<b>Orders for {day_label(store['session'])}</b> (risk gate {gate.state['state']})"]
    out += [f"• {o['side'].title()} {o['sym']} {o['lots']} lots, limit {rp(o['limit'])}" for o in orders] or ["None."]
    if use_slip() and orders:
        out.append("Enter them in your broker app; confirm fills with /fill SYM PRICE LOTS.")
    if events:
        out += ["", "<b>Events</b>"] + ["• " + esc(e) for e in events]
    print("\n".join(out))
    if a.force or orders or events:
        send("\n".join(out), a.dry_run)


# ------------------------------------------------------------------ intraday


def cmd_intraday(a) -> None:
    now = datetime.now(WIB)
    if not a.force and not in_session(now):
        print("Market closed; nothing to check.")
        return
    lp, pp = DATA / "latest.json", DATA / "portfolio.json"
    if not lp.exists() or not pp.exists():
        print("Run the end-of-day job first.")
        return
    latest, st = json.loads(lp.read_text()), json.loads(pp.read_text())
    gate = checkers.RiskGate(DATA / "risk_state.json")
    fills: list[dict] = []
    store = load_store()
    if store:
        for e in approval.collect(store, DATA / "telegram_offset.json", gate, fills) + \
                approval.apply_external([store], DATA / "approvals.json"):
            print("event:", e)
        save_store(store)
    stocks = {s["sym"]: s for s in latest["stocks"]}
    held = list(st["positions"])
    today = now.strftime("%Y-%m-%d")
    q = quotes(held)
    q = {k: v for k, v in q.items() if v[1].strftime("%Y-%m-%d") == today}  # ignore yesterday's bars
    spath = DATA / "alerts_state.json"
    state = json.loads(spath.read_text()) if spath.exists() else {}
    if state.get("date") != today:
        state = dict(date=today, sent=[], log=[])
    alerts: list[str] = []

    def alert(key: str, text: str) -> None:
        if key in state["sent"]:
            return
        state["sent"].append(key)
        state["log"].append(dict(time=now.strftime("%H:%M"), text=text))
        alerts.append(text)

    ihsg = None
    if "IHSG" in q:
        last, ts = q["IHSG"]
        chg = last / latest["regime"]["ihsg_close"] - 1
        ihsg = dict(last=last, chg=chg, time=ts.strftime("%H:%M"))
        gate.update_ihsg(chg)
        for thr in (0.03, 0.05, 0.08):
            if chg <= -thr:
                note = " IDX halts all trading for 30 minutes at −8%." if thr < 0.08 else " That is the trading-halt level."
                alert(f"ihsg{thr}", f"IHSG is down {-chg:.1%} at {last:,.0f}.{note} Risk gate: {gate.state['state']}.")
    rows, exits = [], []
    for sym in held:
        pos, s = st["positions"][sym], stocks.get(sym, {})
        if sym not in q:
            continue
        last, ts = q[sym]
        prev = s.get("close") or pos.get("last")
        rows.append(dict(sym=sym, last=last, chg=last / prev - 1 if prev else 0.0, time=ts.strftime("%H:%M"),
                         stop=pos.get("stop_price"), arb=pos.get("arb_next"), ara=pos.get("ara_next")))
        if pos.get("stop_price") and last < pos["stop_price"]:
            alert(f"{sym}:stop", f"{sym} at {rp(last)} is below its stop level ({rp(pos['stop_price'])}). "
                                 "If it closes there, the bot sells at the next open.")
            exits.append((sym, last, pos))
        if pos.get("arb_next") and last <= pos["arb_next"] * 1.01:
            alert(f"{sym}:arb", f"{sym} at {rp(last)} is at or near its lower price limit ({rp(pos['arb_next'])}). "
                                "Sells may not fill today.")
        if pos.get("ara_next") and last >= pos["ara_next"] * 0.99:
            alert(f"{sym}:ara", f"{sym} at {rp(last)} is at or near its upper price limit ({rp(pos['ara_next'])}).")
    # Intraday exit proposals for the real (slip) account: Ben can act before the close.
    if use_slip() and exits and gate.state["state"] != "HALTED":
        intra = load_store("proposals_intraday.json") or dict(session=today, mode=approval.mode(), items=[])
        if intra.get("session") != today:
            intra = dict(session=today, mode=approval.mode(), items=[])
        ids = {p["id"] for p in intra["items"]}
        fresh = []
        for sym, last, pos in exits:
            ident = approval.pid(today, sym, "sell") + "-intraday"
            if ident in ids:
                continue
            limit = portfolio.floor_tick(last * 0.99)
            p = dict(id=ident, session=today, asof=latest["asof"], created_utc=now_utc(), sym=sym, side="sell",
                     lots=pos["shares"] // 100, shares=pos["shares"], final_shares=pos["shares"], limit=limit,
                     ref_close=last, ara=pos.get("ara_next"), arb=pos.get("arb_next"),
                     why=f"Intraday: below stop {rp(pos['stop_price'])}", rank=None, target_w=0.0, sigd=None,
                     checks=dict(data=True, rules=dict(ok=True, violations=[]), risk=dict(verdict="allow", limit=""), cash=True),
                     committee=dict(verdict="unavailable", size=1.0, reason="", valid=False), blocked=[],
                     status="awaiting", approval=None)
            intra["items"].append(p)
            fresh.append(p)
        save_store(intra, "proposals_intraday.json")
        approval.send_proposals(fresh, rp, esc, a.dry_run)
    watch = sorted(set(held) | {s["sym"] for s in latest["stocks"] if s.get("selected")})
    key = os.environ.get("ANTHROPIC_API_KEY") or None
    _, fresh_news = news.update(DATA / "news.json", watch, key, days=1)
    for it in fresh_news:
        hit = [t for t in it["tickers"] if t in held]
        if hit and abs(it.get("s", 0)) >= 0.5:
            alert("news:" + it["id"], f"{', '.join(hit)} news ({it['s']:+.1f}): {it['title']}")
    (DATA / "intraday.json").write_text(json.dumps(dict(date=today, time=now.strftime("%H:%M"), ihsg=ihsg,
                                                        holdings=rows, alerts=state["log"][-30:],
                                                        gate=gate.state["state"]), separators=(",", ":")))
    spath.write_text(json.dumps(state))
    set_status(last_intraday_utc=now_utc(), risk_gate=gate.state["state"])
    if alerts:
        send("<b>IDX Trend Bot alert</b>\n" + "\n".join("• " + esc(x) for x in alerts), a.dry_run)
    else:
        print("No new alerts.")


# ------------------------------------------------------------------ heartbeat, monitor loop, backtest


def cmd_heartbeat(a) -> None:
    gate = checkers.RiskGate(DATA / "risk_state.json")
    st = json.loads((DATA / "status.json").read_text()) if (DATA / "status.json").exists() else {}
    text = (f"Heartbeat {datetime.now(WIB):%a %d %b %H:%M} WIB. Risk gate {gate.state['state']}. "
            f"Last EOD {st.get('last_eod_utc', 'never')} UTC (prices to {st.get('eod_asof', '?')}). Mode {approval.mode()}.")
    set_status(last_heartbeat_utc=now_utc())
    send(text, a.dry_run)


def cmd_monitor(a) -> None:
    """Always-on runner for a small VM. It does everything the GitHub schedules do, plus a 5-minute
    intraday loop and near-instant pickup of taps and /halt. Set Variable RUNNER=vm on GitHub so the
    scheduled workflows stand down (the approvals workflow keeps running)."""
    import time
    done: set = set()
    while True:
        now = datetime.now(WIB)
        day, hm = now.date(), now.strftime("%H:%M")
        key = (day, hm)
        try:
            job = None
            if hm == "08:30" and ("pre", day) not in done:
                job = ("pre", day, cmd_preopen, "pre-open")
            elif hm in ("08:45", "16:15") and ("hb", key) not in done:
                job = ("hb", key, cmd_heartbeat, "heartbeat")
            elif hm in ("17:30", "19:00") and ("eod", key) not in done:
                job = ("eod", key, cmd_eod, "end of day")
            elif day.day == 1 and hm == "18:30" and ("bt", day) not in done:
                job = ("bt", day, cmd_backtest, "monthly backtest")
            elif in_session(now) and now.minute % 5 == 0 and ("intra", key) not in done:
                job = ("intra", key, cmd_intraday, "intraday")
            elif now.minute % 5 == 0 and 7 <= now.hour <= 23 and ("col", key) not in done:
                job = ("col", key, cmd_collect, "collect")
            if job:
                tag, k, fn, label = job
                done.add((tag, k))
                sync(True)
                fn(a)
                sync(False, f"{label}: {now:%Y-%m-%d %H:%M} WIB")
            if len(done) > 5000:
                done.clear()
        except Exception as exc:
            print("monitor error:", type(exc).__name__, str(exc)[:200])
        time.sleep(15)


def cmd_approve(a) -> None:
    """Called by the approvals workflow when a GitHub issue is opened from the dashboard buttons."""
    d = approval.record_external(DATA / "approvals.json", a.title or "", a.by or "", a.owner or "")
    if d is None:
        print("not an approval issue; ignored")
    elif d.get("ignored"):
        print("ignored:", d["reason"])
    else:
        print(f"recorded {d['action']} for {d['id']} by {d['by']}")
    (DATA / "approve_result.txt").write_text("ignored" if not d or d.get("ignored") else f"{d['action']} {d['id']}")


def cmd_collect(a) -> None:
    """Any time of day: pull Telegram taps/commands and dashboard decisions into the proposal stores."""
    gate = checkers.RiskGate(DATA / "risk_state.json")
    fills: list[dict] = []
    stores = [(n, load_store(n)) for n in ("proposals.json", "proposals_intraday.json")]
    stores = [(n, st) for n, st in stores if st]
    events = []
    for n, st in stores:
        events += approval.collect(st, DATA / "telegram_offset.json", gate, fills)
    events += approval.apply_external([st for _, st in stores], DATA / "approvals.json")
    for n, st in stores:
        save_store(st, n)
    if fills and use_slip():
        fees = engine.FEES.get(os.environ.get("BROKER_FEES") or "ipot")
        done = slip_broker().record_fills(fills, fees)
        events += [f"real fill: {d['side']} {d['sym']} {d['shares'] // 100} lots at {rp(d['price'])}" for d in done]
    for e in events:
        print("event:", e)
    if events and not a.dry_run:
        send("Recorded: " + "; ".join(esc(e) for e in events), False)


def _git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def sync(before: bool, msg: str = "") -> None:
    """On the always-on box: pull the latest state before a job, push docs/data after it."""
    if os.environ.get("GIT_SYNC") != "1":
        return
    if before:
        print("git pull:", _git("pull", "--rebase", "--autostash", "origin", "main")[-200:])
    else:
        _git("add", "docs/data")
        if _git("diff", "--cached", "--quiet") == "":
            pass
        out = _git("commit", "-m", msg or "monitor update")
        if "nothing to commit" not in out:
            _git("pull", "--rebase", "--autostash", "origin", "main")
            print("git push:", _git("push", "origin", "main")[-200:])


def cmd_backtest(a) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    engine.configure(v2=(os.environ.get("RULES") or "v2").lower() == "v2", broker=os.environ.get("BROKER_FEES") or None)
    engine.run(load_raw(a), True, DATA)
    set_status(last_backtest_utc=now_utc())


def main() -> None:
    ap = argparse.ArgumentParser(description="IDX Trend Bot v2 scheduled jobs")
    ap.add_argument("job", choices=["eod", "preopen", "intraday", "heartbeat", "monitor", "backtest", "approve", "collect"])
    ap.add_argument("--title", help="approve: the GitHub issue title, e.g. approve:2026-09-28-AKRA-buy")
    ap.add_argument("--by", help="approve: the GitHub login that opened the issue")
    ap.add_argument("--owner", help="approve: the repository owner login")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    ap.add_argument("--cache", help="read daily prices from a pickle (testing)")
    ap.add_argument("--force", action="store_true", help="run even outside market hours / on old data")
    a = ap.parse_args()
    {"eod": cmd_eod, "preopen": cmd_preopen, "intraday": cmd_intraday, "heartbeat": cmd_heartbeat,
     "monitor": cmd_monitor, "backtest": cmd_backtest, "approve": cmd_approve, "collect": cmd_collect}[a.job](a)


if __name__ == "__main__":
    main()
