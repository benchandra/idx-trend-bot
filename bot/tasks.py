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
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import engine, kronos_signal, news, portfolio

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


def kronos_lines(kr: dict | None, st: dict) -> list[str]:
    """Short 'Kronos view' block: the index first, then holdings and selected names. Advisory only."""
    if not kr:
        return []
    if not kr.get("ok"):
        return ["", f"<b>Kronos view</b>: not available ({esc(kr.get('reason', 'unknown'))})."]
    rows = [r for r in kr["names"] if r["sym"] == "IHSG" or r.get("held") or r.get("selected")][:9]
    out = ["", f"<b>Kronos view, next {kr['horizon']} sessions</b> (advisory, {kr['paths']} sampled paths each)"]
    for r in rows:
        name = "IHSG" if r["sym"] == "IHSG" else r["sym"]
        vol = {"wider": "Sees more risk than GARCH", "tighter": "Sees less risk than GARCH",
               "agrees": "Risk view matches GARCH"}.get(r.get("vol_flag") or "", "")
        out.append(f"• {name}: {r['p_up5']:.0%} of paths up, median {r['ret5_med']:+.1%} "
                   f"(range {r['ret5_p10']:+.1%} to {r['ret5_p90']:+.1%}){'. ' + vol if vol else ''}")
    out.append("Orders above come from the rulebook only; Kronos is logged for testing.")
    return out


def eod_message(latest: dict, st: dict, ev: dict, summ: dict, kr: dict | None = None) -> str:
    g, hist = latest["regime"], st["history"]
    eq, start = hist[-1]["equity"], st["start_cash"]
    sv = latest.get("svi") or {}
    out = [f"<b>IDX Trend Bot: {day_label(latest['asof'])} close</b>",
           f"IHSG {g['ihsg_close']:,.0f} ({g['ihsg_change']:+.2%}). " +
           ("Market brake off." if g["above"] else "Market brake on: at most 50% invested.") +
           (f" Stress gauge {sv['value']:.2f} ({sv['label']}{', MSCI event window' if sv.get('event_window') else ''}"
            f"{': invested at most ' + format(g['cap'], '.0%') if sv.get('applied') else '; shown only'})." if sv.get("value") is not None else ""),
           f"Model account {rp(eq)} ({eq / start - 1:+.2%} since {day_label(st['start_date'])}), "
           f"{hist[-1]['invested']:.0%} invested. Paper money only."]
    if ev["filled"]:
        out += ["", "<b>Filled today</b>"] + [
            f"• {'Bought' if t['side'] == 'buy' else 'Sold'} {t['sym']} {t['shares'] // 100} lots at {rp(t['price'])}"
            for t in ev["filled"]]
    if ev["unfilled"]:
        out += ["", "<b>Not filled</b>"] + [f"• {o['side'].title()} {o['sym']}: {esc(o['note'])}" for o in ev["unfilled"]]
    out += ["", f"<b>Orders for {day_label(latest['next_session'])}</b>"]
    if st["pending"]:
        out += [f"• {o['side'].title()} {o['sym']} {o['lots']} lots, limit {rp(o['limit'])}. {esc(o['why'])}"
                for o in st["pending"]]
    elif latest["rebalance_today"]:
        out.append("None: the holdings already match the targets.")
    else:
        out.append(f"None. Next rebalance after the {day_label(latest['next_rebalance_close'])} close.")
    lines = []
    for t in list(st["positions"]) + ["MARKET"]:
        d = summ.get(t)
        if not d:
            continue
        pick = d["worst"] if abs(d["worst"]["s"]) >= abs(d["best"]["s"]) else d["best"]
        lines.append(f"• {'Market' if t == 'MARKET' else t} {d['avg']:+.2f} ({d['n']} headlines): {esc(pick['title'][:110])}")
    if lines:
        out += ["", "<b>News mood, last 24h</b> (−1 to +1)"] + lines
    out += kronos_lines(kr, st)
    url = dashboard_url()
    if url:
        out += ["", f'<a href="{url}">Open the dashboard</a>']
    return "\n".join(out)


def cmd_eod(a) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    raw = load_raw(a)
    ppath = DATA / "portfolio.json"
    held = list(json.loads(ppath.read_text()).get("positions", {})) if ppath.exists() else []
    engine.configure(os.environ.get("BOT_RULES", "v2"))  # v2 in production; the stress gauge is computed and shown
    engine.run(raw, False, DATA, held=held)
    latest = json.loads((DATA / "latest.json").read_text())
    st = portfolio.load(ppath, latest["asof"], float(os.environ.get("START_CASH") or 100_000_000))
    st, ev, new_day = portfolio.process_day(st, raw, latest)
    portfolio.save(ppath, st)
    key = os.environ.get("ANTHROPIC_API_KEY") or None
    items, _ = news.update(DATA / "news.json", engine.UNIVERSE, key)
    summ = news.summary(items, 24)
    if new_day:
        news.log_daily(DATA / "sentiment_log.csv", latest["asof"], summ)
    kr = None
    if (os.environ.get("KRONOS") or "on").lower() not in ("off", "0", "false"):
        try:
            kr = kronos_signal.run(raw, latest, DATA, held=list(st["positions"]))
        except Exception as e:  # Kronos is optional: a failure here must not stop orders or alerts
            print("Kronos failed:", repr(e))
            kr = dict(ok=False, reason=str(e)[:120])
            (DATA / "kronos.json").write_text(json.dumps(kr))
    set_status(last_eod_utc=now_utc(), eod_asof=latest["asof"], sentiment_method="claude" if key else "keywords",
               kronos=("ok " + (kr.get("asof") or "")) if kr and kr.get("ok") else ("off" if kr is None else "failed"))
    if new_day or a.force:
        send(eod_message(latest, st, ev, summ, kr), a.dry_run)
    else:
        print("No new session since the last run (holiday, or prices not published yet). Nothing sent.")


# ------------------------------------------------------------------ intraday


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
        for thr in (0.03, 0.05, 0.08):
            if chg <= -thr:
                note = " IDX halts all trading for 30 minutes at −8%." if thr < 0.08 else " That is the trading-halt level."
                alert(f"ihsg{thr}", f"IHSG is down {-chg:.1%} at {last:,.0f}.{note}")
    rows = []
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
        if pos.get("arb_next") and last <= pos["arb_next"] * 1.01:
            alert(f"{sym}:arb", f"{sym} at {rp(last)} is at or near its lower price limit ({rp(pos['arb_next'])}). "
                                "Sells may not fill today.")
        if pos.get("ara_next") and last >= pos["ara_next"] * 0.99:
            alert(f"{sym}:ara", f"{sym} at {rp(last)} is at or near its upper price limit ({rp(pos['ara_next'])}).")
    watch = sorted(set(held) | {s["sym"] for s in latest["stocks"] if s.get("selected")})
    key = os.environ.get("ANTHROPIC_API_KEY") or None
    _, fresh = news.update(DATA / "news.json", watch, key, days=1)
    for it in fresh:
        hit = [t for t in it["tickers"] if t in held]
        if hit and abs(it.get("s", 0)) >= 0.5:
            alert("news:" + it["id"], f"{', '.join(hit)} news ({it['s']:+.1f}): {it['title']}")
    (DATA / "intraday.json").write_text(json.dumps(dict(date=today, time=now.strftime("%H:%M"), ihsg=ihsg,
                                                        holdings=rows, alerts=state["log"][-30:]),
                                                   separators=(",", ":")))
    spath.write_text(json.dumps(state))
    set_status(last_intraday_utc=now_utc())
    if alerts:
        send("<b>IDX Trend Bot alert</b>\n" + "\n".join("• " + esc(x) for x in alerts), a.dry_run)
    else:
        print("No new alerts.")


# ------------------------------------------------------------------ backtest


def cmd_seed(a) -> None:
    """Start (or restart) the model account from real holdings, so the bot's orders apply to what you actually own.
    --positions "BBCA 43 6562, BMRI 66 4956" = symbol, lots, average price. --cash = free cash in the account.
    Names outside the bot's universe (GOTO, AADI, ...) are kept as unmanaged holdings: valued, never traded."""
    lp = DATA / "latest.json"
    latest = json.loads(lp.read_text()) if lp.exists() else None
    close = {x["sym"]: x["close"] for x in latest["stocks"]} if latest else {}
    day = latest["asof"] if latest else datetime.now(WIB).strftime("%Y-%m-%d")
    st = portfolio.new_state(day, float(a.cash))
    skipped = []
    for item in [x.strip() for x in a.positions.replace(";", ",").split(",") if x.strip()]:
        parts = item.replace("@", " ").split()
        if len(parts) != 3:
            raise SystemExit(f"Cannot read '{item}': use SYMBOL LOTS AVERAGE_PRICE, e.g. BBCA 43 6562")
        sym, lots, avg = parts[0].upper(), float(parts[1]), float(parts[2])  # lots may be fractional (odd lots)
        shares, c = int(round(lots * 100)), close.get(sym, avg)
        managed = sym in engine.UNIVERSE
        if not managed:
            skipped.append(sym)
        st["positions"][sym] = dict(shares=shares, cost=shares * avg, entry=day, peak=max(avg, c), last=c, managed=managed)
    st["start_cash"] = float(a.cash) + sum(x["cost"] for x in st["positions"].values())
    st["note"] = f"seeded from real holdings on {day}" + (f"; held but not managed by the bot: {', '.join(skipped)}" if skipped else "")
    DATA.mkdir(parents=True, exist_ok=True)
    portfolio.save(DATA / "portfolio.json", st)
    print(f"seeded {len(st['positions'])} positions, cash {a.cash:,.0f}, cost basis {st['start_cash']:,.0f}; unmanaged {skipped}")


def cmd_backtest(a) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    raw = load_raw(a)
    for mode, suffix in (("v1", "_v1"), ("v3", "_v3"), ("v2", "")):  # every variant tried: the DSR counts them
        engine.configure(mode)
        engine.run(raw, True, DATA, suffix=suffix, trials_path=DATA / "trials.json")
    set_status(last_backtest_utc=now_utc())


def main() -> None:
    ap = argparse.ArgumentParser(description="IDX Trend Bot scheduled jobs")
    ap.add_argument("job", choices=["eod", "intraday", "backtest", "seed"])
    ap.add_argument("--positions", default="", help='seed: "BBCA 43 6562, BMRI 66 4956" (symbol, lots, average price)')
    ap.add_argument("--cash", type=float, default=0.0, help="seed: free cash in the account")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending them")
    ap.add_argument("--cache", help="read daily prices from a pickle (testing)")
    ap.add_argument("--force", action="store_true", help="run even outside market hours / on old data")
    a = ap.parse_args()
    {"eod": cmd_eod, "intraday": cmd_intraday, "backtest": cmd_backtest, "seed": cmd_seed}[a.job](a)


if __name__ == "__main__":
    main()
