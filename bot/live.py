#!/usr/bin/env python3
"""Near-live quotes for the dashboard.

  python -m bot.live                  one pass: writes docs/data/live.json (and pushes it to GitHub if configured)
  python -m bot.live --every 60       keeps running: one pass a minute during the session, idle outside it

Runs on anything with Python and internet that stays on: a free always-on VM, a Raspberry Pi, a laptop.
Prices are Yahoo Finance 5-minute bars, usually a few minutes behind the exchange. Near-live, not tick data.

Pushing to the dashboard from another machine (optional, two environment variables):
  GITHUB_REPO    for example  ben/idx-trend-bot
  GITHUB_TOKEN   a fine-grained personal access token with "Contents: read and write" on that repository only
The file goes to a separate branch (default "live") so GitHub Pages is NOT rebuilt on every push; the page reads
it straight from raw.githubusercontent.com, which can lag up to 5 minutes. Without the variables the file is only
written locally (docs/data/live.json), which is what the intraday GitHub Actions job also does.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import tasks

DATA = tasks.DATA
RAW = "https://raw.githubusercontent.com/{repo}/main/docs/data/{name}"


def load_state() -> tuple[dict | None, dict | None]:
    """latest.json and portfolio.json from this folder, or from the repository's main branch."""
    latest = portfolio = None
    lp, pp = DATA / "latest.json", DATA / "portfolio.json"
    if lp.exists():
        latest = json.loads(lp.read_text())
        portfolio = json.loads(pp.read_text()) if pp.exists() else None
        return latest, portfolio
    repo = os.environ.get("GITHUB_REPO")
    if repo:
        import requests

        for name in ("latest.json", "portfolio.json"):
            r = requests.get(RAW.format(repo=repo, name=name), timeout=30)
            if r.status_code == 200:
                if name == "latest.json":
                    latest = r.json()
                else:
                    portfolio = r.json()
    return latest, portfolio


def one_pass(latest: dict, st: dict | None) -> dict:
    now = datetime.now(tasks.WIB)
    stocks = {s["sym"]: s for s in latest["stocks"]}
    positions = (st or {}).get("positions", {})
    watch = sorted(set(positions) | {s for s, v in stocks.items() if v.get("selected")})
    q = tasks.quotes(watch)
    today = now.strftime("%Y-%m-%d")
    out = dict(date=today, time=now.strftime("%H:%M"), updated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
               source="Yahoo Finance 5-minute bars", asof_close=latest["asof"], ihsg=None, quotes=[], equity_est=None)
    if "IHSG" in q:
        last, ts = q["IHSG"]
        out["ihsg"] = dict(last=last, chg=last / latest["regime"]["ihsg_close"] - 1, time=ts.strftime("%H:%M"),
                           bar_date=ts.strftime("%Y-%m-%d"))
    value = 0.0
    for sym in watch:
        if sym not in q:
            continue
        last, ts = q[sym]
        s, pos = stocks.get(sym, {}), positions.get(sym, {})
        prev = s.get("close") or pos.get("last")
        out["quotes"].append(dict(sym=sym, last=last, chg=last / prev - 1 if prev else 0.0, time=ts.strftime("%H:%M"),
                                  bar_date=ts.strftime("%Y-%m-%d"), held=sym in positions, shares=pos.get("shares"),
                                  stop=pos.get("stop_price"), arb=pos.get("arb_next"), ara=pos.get("ara_next")))
        if sym in positions:
            value += pos.get("shares", 0) * last
    if st:
        out["equity_est"] = st.get("cash", 0.0) + value
    return out


def push(path: Path, branch: str) -> str:
    """Upload the file to GitHub on `branch` through the contents API (no git needed). Returns a short status."""
    repo, token = os.environ.get("GITHUB_REPO"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return "not pushed (GITHUB_REPO / GITHUB_TOKEN not set)"
    import requests

    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if requests.get(f"{api}/branches/{branch}", headers=h, timeout=30).status_code == 404:
        main = requests.get(f"{api}/git/ref/heads/main", headers=h, timeout=30).json()
        requests.post(f"{api}/git/refs", headers=h, timeout=30, json={"ref": f"refs/heads/{branch}", "sha": main["object"]["sha"]})
    url = f"{api}/contents/live.json"
    cur = requests.get(url, headers=h, params={"ref": branch}, timeout=30)
    body = {"message": "live " + datetime.now(tasks.WIB).strftime("%Y-%m-%d %H:%M"), "branch": branch,
            "content": base64.b64encode(path.read_bytes()).decode()}
    if cur.status_code == 200:
        body["sha"] = cur.json()["sha"]
    r = requests.put(url, headers=h, json=body, timeout=30)
    return f"pushed to {branch}" if r.status_code in (200, 201) else f"push failed {r.status_code}: {r.text[:120]}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Near-live quotes for the IDX Trend Bot dashboard")
    ap.add_argument("--every", type=int, default=0, help="seconds between passes; 0 = one pass and exit")
    ap.add_argument("--branch", default=os.environ.get("LIVE_BRANCH") or "live")
    ap.add_argument("--force", action="store_true", help="run even outside market hours")
    a = ap.parse_args()
    while True:
        now = datetime.now(tasks.WIB)
        if a.force or tasks.in_session(now):
            try:
                latest, st = load_state()
                if not latest:
                    print("No latest.json yet: run the End of day job first.")
                else:
                    out = one_pass(latest, st)
                    DATA.mkdir(parents=True, exist_ok=True)
                    p = DATA / "live.json"
                    p.write_text(json.dumps(out, separators=(",", ":")))
                    print(now.strftime("%H:%M"), f"{len(out['quotes'])} quotes", out["ihsg"] and f"IHSG {out['ihsg']['last']:,.0f}",
                          push(p, a.branch))
            except Exception:
                traceback.print_exc()
        else:
            print(now.strftime("%H:%M"), "market closed")
        if not a.every:
            break
        time.sleep(a.every if tasks.in_session(now) or a.force else max(a.every, 300))


if __name__ == "__main__":
    main()
