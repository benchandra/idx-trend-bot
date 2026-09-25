#!/usr/bin/env python3
"""IDX Trend Bot - engine v1.0 (built with Claude, September 2026).

Run after the 16:15 WIB close:
  python engine.py              -> out/latest.json   (today's signals)
  python engine.py --backtest   -> also out/backtest.json (walk-forward test)

Steps: download daily prices (Yahoo Finance) -> GJR-GARCH(1,1) with Hansen
skewed-t shocks per stock (arch) -> rulebook (trend filter, risk-adjusted
ranking, inverse-vol sizing, 15% portfolio vol target, IHSG brake, weekly
rebalance, daily stops) -> JSON for the dashboard.

Update routine (for Claude): read_db bot/engine -> save field 'source' as
engine.py -> pip install arch yfinance scipy -> python engine.py [--backtest]
-> write_db bot/latest (and bot/backtest) from out/*.json with file_path.
Research tool, not financial advice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import warnings
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

VERSION = "2.0"
UNIVERSE = (
    "BBCA BBRI BMRI BBNI BRIS TLKM ISAT EXCL TOWR ASII UNTR UNVR ICBP INDF MYOR "
    "KLBF CPIN JPFA AMRT MAPI ACES ADRO PTBA ITMG MEDC PGAS AKRA ANTM INCO SMGR"
).split()
INDEX = "^JKSE"
P = dict(
    n_hold=8, max_w=0.20, vol_target=0.15, sma_len=200, mom_len=126,
    stop_k=2.5, stop_days=5, band=0.02, bear_cap=0.50, dpy=240,
    min_hist=252, corr_len=252, liq_len=60, liq_min_value=10e9,
    buy_fee=0.0019, sell_fee=0.0029, slippage=0.001,
    buy_buffer=0.01, sell_buffer=0.02,
    start_capital=100_000_000, bt_start="2016-01-01",
    hold_rank=0, resize_frac=0.0, broker="ipot",  # v2: hold_rank=16, resize_frac=0.5
)
# All-in online fees (buy, sell) incl. levy, VAT and the 0.1% sales tax. Research of Sept 2026.
FEES = {"ipot": (0.0019, 0.0029), "stockbit": (0.0015, 0.0025), "ajaib": (0.001513, 0.002513),
        "mirae": (0.00149, 0.00249), "bni": (0.0017, 0.0027)}


def configure(v2: bool = False, broker: str | None = None) -> None:
    """Switch on the v2 rules (trial 2) and/or a broker fee profile before run()."""
    if v2:
        P.update(hold_rank=16, resize_frac=0.5)
    if broker:
        P["broker"] = broker
        P["buy_fee"], P["sell_fee"] = FEES[broker]


def select(order: list[int], held: set[int]) -> list[int]:
    """v1: the top n_hold by score. v2 (hold_rank > 0): keep a held name while it ranks within
    hold_rank (and is still eligible), fill the remaining slots from the top. Order = rank order."""
    top = order[: P["n_hold"]]
    if not P["hold_rank"]:
        return top
    keep = [j for j in order[: P["hold_rank"]] if j in held]
    fill = [j for j in top if j not in keep][: max(0, P["n_hold"] - len(keep))]
    return sorted(keep + fill, key=order.index)
# IDX auto-rejection regimes (effective date, ARA by band, ARB by band);
# bands by reference price: <=200, <=5000, >5000. Research of Sept 2026;
# the 2017 switch date is approximate (decree effective late Dec 2016/Jan 2017).
AR_REGIMES = [
    ("2000-01-01", (0.35, 0.25, 0.20), (0.35, 0.25, 0.20)),
    ("2015-08-25", (0.35, 0.25, 0.20), (0.10, 0.10, 0.10)),
    ("2017-01-01", (0.35, 0.25, 0.20), (0.35, 0.25, 0.20)),
    ("2020-03-10", (0.35, 0.25, 0.20), (0.10, 0.10, 0.10)),
    ("2020-03-13", (0.35, 0.25, 0.20), (0.07, 0.07, 0.07)),
    ("2023-06-05", (0.35, 0.25, 0.20), (0.15, 0.15, 0.15)),
    ("2023-09-04", (0.35, 0.25, 0.20), (0.35, 0.25, 0.20)),
    ("2025-04-08", (0.35, 0.25, 0.20), (0.15, 0.15, 0.15)),
    ("2027-01-01", (0.35, 0.25, 0.20), (0.35, 0.25, 0.20)),
]
AR_DATES = np.array([np.datetime64(d, "ns") for d, _, _ in AR_REGIMES])
# Weekday exchange closures (IDX Kalender Bursa 2026). 2027 not yet published.
HOLIDAYS = {
    "2026-01-01", "2026-01-16", "2026-02-16", "2026-02-17", "2026-03-18",
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24", "2026-04-03",
    "2026-05-01", "2026-05-14", "2026-05-15", "2026-05-27", "2026-05-28",
    "2026-06-01", "2026-06-16", "2026-08-17", "2026-08-25", "2026-12-24",
    "2026-12-25", "2026-12-31",
}


def ar_pcts(regime: int, ref: float) -> tuple[float, float]:
    band = 0 if ref <= 200 else (1 if ref <= 5000 else 2)
    _, ara, arb = AR_REGIMES[regime]
    return ara[band], arb[band]


# ----------------------------------------------------------------- data


def download(start: str = "2014-01-01") -> pd.DataFrame:
    import yfinance as yf

    tickers = [s + ".JK" for s in UNIVERSE] + [INDEX]
    raw = yf.download(tickers, start=start, auto_adjust=False, actions=False,
                      progress=False, threads=True)
    if raw is None or raw.empty:
        raise SystemExit("Download failed: Yahoo Finance returned no data.")
    return raw


def prepare(raw: pd.DataFrame) -> dict:
    cal = raw["Close"][INDEX].dropna().index
    cal = cal[cal.dayofweek < 5]
    syms = [s + ".JK" for s in UNIVERSE]

    def grab(field):
        df = raw[field][syms].reindex(cal)
        df.columns = UNIVERSE
        return df

    C, A, O, V = grab("Close"), grab("Adj Close"), grab("Open"), grab("Volume")
    trade = C.notna() & V.gt(0)
    Cf, Af = C.ffill(), A.ffill()
    listed = Af.notna()
    ret_raw = Af.pct_change()
    clipped = int(((ret_raw.abs() > 0.35) & listed).sum().sum())
    # IDX cannot move a stock more than 35% in a day: larger jumps are data errors
    ret = ret_raw.clip(-0.35, 0.35).where(listed)
    cp = (1 + ret.fillna(0)).cumprod().where(listed)
    A_clean = cp * (Af.iloc[-1] / cp.iloc[-1])  # anchored to latest real price
    oc = (O / C).where(trade).fillna(1.0)
    ihsg = raw["Close"][INDEX].reindex(cal).ffill()
    return dict(cal=cal, C=Cf, A=A_clean, AO=A_clean * oc, O=O.where(trade),
                ret=ret, trade=trade, value=(C * V).where(trade), ihsg=ihsg,
                clipped=clipped)


# --------------------------------------------------------------- GARCH


def _models(r):
    from arch import arch_model

    return [
        arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1, dist="skewt", rescale=False),
        arch_model(r, mean="Constant", vol="GARCH", p=1, o=0, q=1, dist="t", rescale=False),
    ]


def _fit(models, **kw):
    for am in models:
        try:
            res = am.fit(disp="off", options={"maxiter": 1000}, **kw)
        except Exception:
            continue
        if res.convergence_flag == 0 and np.all(np.isfinite(res.params.values)):
            return am, res
    return None, None


def _q01(am, res) -> float:
    names = am.distribution.parameter_names()
    return float(am.distribution.ppf(0.01, np.asarray(res.params[names]) if names else None))


def _ewma(r: pd.Series, lam: float = 0.94) -> pd.Series:
    v = np.empty(len(r))
    v[0] = float(np.var(r.iloc[:60]))
    x = r.values
    for t in range(1, len(r)):
        v[t] = lam * v[t - 1] + (1 - lam) * x[t - 1] ** 2
    # v[t] is the forecast for day t made at t-1; shift so f[t] = forecast for t+1
    nxt = lam * v + (1 - lam) * x ** 2
    return pd.Series(nxt, index=r.index)


def walkforward(args) -> dict:
    """Yearly refits on data before each year; out-of-sample 1-day variance forecasts."""
    name, r, years = args
    r = r.dropna()
    out = dict(name=name, f=pd.Series(dtype=float), mu=pd.Series(dtype=float),
               q=pd.Series(dtype=float), models={})
    if len(r) < 500:
        return out
    models = _models(r)
    ew = None
    f, mu, q = (pd.Series(np.nan, index=r.index) for _ in range(3))
    for y in years:
        y0, y1 = pd.Timestamp(y, 1, 1), pd.Timestamp(y + 1, 1, 1)
        pre = r.index[r.index < y0]
        inyr = r.index[(r.index >= y0) & (r.index < y1)]
        if len(pre) < 500 or len(inyr) == 0:
            continue
        start = pre[-1]
        am, res = _fit(models, last_obs=y0)
        if res is None:
            ew = _ewma(r) if ew is None else ew
            blk = ew.loc[start:inyr[-1]]
            f.loc[blk.index] = blk.values
            mu.loc[blk.index] = 0.0
            q.loc[blk.index] = -2.326
            out["models"][y] = "ewma"
            continue
        fc = res.forecast(horizon=1, start=start, reindex=False).variance["h.1"]
        blk = fc.loc[start:inyr[-1]]
        f.loc[blk.index] = blk.values
        mu.loc[blk.index] = float(res.params["mu"])
        q.loc[blk.index] = _q01(am, res)
        out["models"][y] = f"{'gjr' if am.volatility.o else 'garch'}-{am.distribution.name}"
    out.update(f=f, mu=mu, q=q)
    return out


def live_fit(args) -> dict:
    """Fit on all data; forecast tomorrow's variance."""
    name, r = args
    r = r.dropna()
    out = dict(name=name, var1=np.nan, z=pd.Series(dtype=float), model="none", params={})
    if len(r) < 500:
        return out
    am, res = _fit(_models(r))
    if res is None:
        ew = _ewma(r)
        out.update(var1=float(ew.iloc[-1]), z=r / np.sqrt(ew.shift(1)), model="ewma")
        return out
    var1 = float(res.forecast(horizon=1, reindex=False).variance.iloc[-1, 0])
    prm = {k: round(float(v), 5) for k, v in res.params.items()}
    out.update(var1=var1, z=res.std_resid, params=prm,
               model=f"{'gjr' if am.volatility.o else 'garch'}-{am.distribution.name}")
    return out


# ------------------------------------------------------------ portfolio


def cap_weights(w: np.ndarray, cap: float) -> np.ndarray:
    w = w.copy()
    for _ in range(50):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        under = w < cap - 1e-12
        if not under.any():
            break
        w[under] += excess * w[under] / w[under].sum()
    return w


def target_weights(sel, sd, Zwin, cap):
    """Inverse-vol weights, 20% cap, scaled to the vol target; gross <= cap."""
    if len(sel) == 0:
        return np.array([]), float("nan"), 0.0
    w = (1 / sd) / (1 / sd).sum()
    w = cap_weights(w, P["max_w"])
    R = pd.DataFrame(Zwin).corr(min_periods=60).values
    off = R[~np.eye(len(sel), dtype=bool)]
    fill = float(np.nanmean(off)) if np.isfinite(off).any() else 0.3
    R = np.where(np.isfinite(R), R, fill)
    np.fill_diagonal(R, 1.0)
    S = np.outer(sd, sd) * R
    pv = math.sqrt(max(float(w @ S @ w), 1e-12))
    k = min(cap / w.sum(), (P["vol_target"] / math.sqrt(P["dpy"])) / pv)
    return w * k, pv * math.sqrt(P["dpy"]), k


# ------------------------------------------------------------- stats


def perf(eq: pd.Series) -> dict:
    from scipy.stats import kurtosis, norm, skew

    r = eq.pct_change().dropna()
    n = len(r)
    yrs = n / P["dpy"]
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    sd = r.std(ddof=1)
    srp = r.mean() / sd
    g3, g4 = float(skew(r)), float(kurtosis(r, fisher=False))
    psr = float(norm.cdf(srp * math.sqrt(n - 1) / math.sqrt(max(1 - g3 * srp + (g4 - 1) / 4 * srp**2, 1e-9))))
    dd = eq / eq.cummax() - 1
    return dict(cagr=cagr, vol=sd * math.sqrt(P["dpy"]), sharpe=srp * math.sqrt(P["dpy"]),
                max_dd=float(dd.min()), calmar=cagr / abs(dd.min()) if dd.min() < 0 else None,
                psr=psr, total=float(eq.iloc[-1] / eq.iloc[0] - 1))


def kupiec(hits: np.ndarray, p: float = 0.01) -> dict:
    from scipy.stats import chi2

    n, x = len(hits), int(hits.sum())
    ph = x / n if n else 0
    ll0 = (n - x) * math.log(1 - p) + x * math.log(p)
    ll1 = ((n - x) * math.log(1 - ph) if ph < 1 else 0) + (x * math.log(ph) if x > 0 else 0)
    lr = -2 * (ll0 - ll1)
    return dict(days=n, exceedances=x, expected=round(n * p, 1), lr=round(lr, 3),
                p_value=round(float(1 - chi2.cdf(lr, 1)), 4))


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 6)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# -------------------------------------------------------------- engine


def next_session(day: pd.Timestamp) -> pd.Timestamp:
    d = day + timedelta(days=1)
    while d.dayofweek >= 5 or d.strftime("%Y-%m-%d") in HOLIDAYS:
        d += timedelta(days=1)
    return d


def run(raw: pd.DataFrame, do_backtest: bool, outdir: Path, holdings: list[str] | None = None) -> None:
    d = prepare(raw)
    held_idx = {UNIVERSE.index(s) for s in (holdings or []) if s in UNIVERSE}
    cal, A, C, trade = d["cal"], d["A"], d["C"], d["trade"]
    n = len(UNIVERSE)
    rpct = {s: 100 * np.log1p(d["ret"][s].where(trade[s])) for s in UNIVERSE}
    ihsg_r = 100 * np.log(d["ihsg"] / d["ihsg"].shift(1))
    years = list(range(2016, cal[-1].year + 1))
    workers = max(1, min(8, (os.cpu_count() or 2)))

    sma = A.rolling(P["sma_len"], min_periods=P["sma_len"]).mean()
    mom = A / A.shift(P["mom_len"]) - 1
    hist = A.notna().cumsum()
    liq = d["value"].rolling(P["liq_len"], min_periods=20).median()
    ihsg_sma = d["ihsg"].rolling(200, min_periods=200).mean()
    ihsg_cap = np.where(d["ihsg"] > ihsg_sma, 1.0, P["bear_cap"])
    iso = cal.isocalendar()
    wk = list(zip(iso["year"], iso["week"]))
    rc = C / C.shift(1) - 1  # close-to-close move, used to spot limit locks
    reg_idx = np.searchsorted(AR_DATES, cal.values, side="right") - 1

    def lock_flags(i):
        ara_l, arb_l = np.zeros(n, bool), np.zeros(n, bool)
        for j, s in enumerate(UNIVERSE):
            ref = C[s].iloc[i - 1]
            if not np.isfinite(ref) or not trade[s].iloc[i]:
                continue
            a, b = ar_pcts(reg_idx[i], ref)
            ara_l[j] = rc[s].iloc[i] >= a - 0.01
            arb_l[j] = rc[s].iloc[i] <= -(b - 0.01)
        return ara_l, arb_l

    # ---------------- live snapshot
    with ProcessPoolExecutor(workers) as ex:
        live = list(ex.map(live_fit, [(s, rpct[s]) for s in UNIVERSE] + [("IHSG", ihsg_r)]))
    L = {o["name"]: o for o in live}
    i = len(cal) - 1
    asof = cal[i]
    sd_live = np.array([math.sqrt(L[s]["var1"]) / 100 if np.isfinite(L[s]["var1"]) else np.nan for s in UNIVERSE])
    Zl = pd.DataFrame({s: L[s]["z"] for s in UNIVERSE}).reindex(cal)
    elig_live = ((A.iloc[i] > sma.iloc[i]) & (mom.iloc[i] > 0) & (hist.iloc[i] >= P["min_hist"])
                 & (liq.iloc[i] >= P["liq_min_value"]) & trade.iloc[i]).values & np.isfinite(sd_live)
    score_live = np.where(elig_live, mom.iloc[i].values / (sd_live * math.sqrt(P["dpy"])), np.nan)
    cand = [j for j in range(n) if elig_live[j]]
    order = sorted(cand, key=lambda j: -score_live[j])
    sel = select(order, held_idx)
    w, pv, k = target_weights(sel, sd_live[sel], Zl.iloc[-P["corr_len"]:, sel].values, float(ihsg_cap[i]))
    tw = np.zeros(n)
    tw[sel] = w
    ara_l, arb_l = lock_flags(i)
    nxt = next_session(asof)
    rebalance_today = wk[i] != tuple(nxt.isocalendar()[:2])
    d2 = asof
    while not rebalance_today:
        n2 = next_session(d2)
        if tuple(n2.isocalendar()[:2]) != tuple(next_session(n2).isocalendar()[:2]):
            d2 = n2
            break
        d2 = n2
    ihsg_var = L["IHSG"]["var1"]
    wf_ihsg = walkforward(("IHSG", ihsg_r, years))
    hist5 = np.sqrt(wf_ihsg["f"].dropna().iloc[-5 * P["dpy"]:]) / 100 * math.sqrt(P["dpy"])
    ihsg_vol = math.sqrt(ihsg_var) / 100 * math.sqrt(P["dpy"])
    stocks = []
    for j, s in enumerate(UNIVERSE):
        if not trade[s].iloc[i]:
            status = "Not traded today (suspended or no data)"
        elif not np.isfinite(sd_live[j]):
            status = "Too little history for the model"
        elif hist.iloc[i][s] < P["min_hist"]:
            status = "Less than one year of history"
        elif not A.iloc[i][s] > sma.iloc[i][s]:
            status = "Below its 200-day average"
        elif not mom.iloc[i][s] > 0:
            status = "6-month return is negative"
        elif not liq.iloc[i][s] >= P["liq_min_value"]:
            status = "Trading value too low"
        elif j in sel:
            status = f"Selected (rank {order.index(j) + 1})" + (" — retained" if order.index(j) >= P["n_hold"] else "")
        else:
            status = f"Uptrend, ranked {order.index(j) + 1}: outside the top {P['n_hold']}"
        stocks.append(dict(
            sym=s, close=C[s].iloc[i], prev_close=C[s].iloc[i - 1], sma200_ok=bool(A.iloc[i][s] > sma.iloc[i][s]),
            mom126=mom.iloc[i][s], sigd=sd_live[j], siga=sd_live[j] * math.sqrt(P["dpy"]),
            score=score_live[j], eligible=bool(elig_live[j]), selected=j in sel,
            rank=order.index(j) + 1 if j in order else None, target_w=tw[j], status=status,
            ara_locked=bool(ara_l[j]), arb_locked=bool(arb_l[j]),
            stop_mult=1 - P["stop_k"] * sd_live[j] * math.sqrt(P["stop_days"]) if np.isfinite(sd_live[j]) else None,
            liq_bn=liq.iloc[i][s] / 1e9, model=L[s]["model"],
        ))
    recent = cal[-300:]
    latest = dict(
        version=VERSION, generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        asof=asof.strftime("%Y-%m-%d"), next_session=nxt.strftime("%Y-%m-%d"),
        rebalance_today=bool(rebalance_today), next_rebalance_close=d2.strftime("%Y-%m-%d"),
        params=P, universe=UNIVERSE,
        regime=dict(ihsg_close=d["ihsg"].iloc[i], ihsg_sma200=ihsg_sma.iloc[i],
                    above=bool(d["ihsg"].iloc[i] > ihsg_sma.iloc[i]), cap=float(ihsg_cap[i]),
                    ihsg_vol_ann=ihsg_vol, ihsg_vol_pctile_5y=float((hist5 < ihsg_vol).mean()),
                    ihsg_change=float(d["ihsg"].iloc[i] / d["ihsg"].iloc[i - 1] - 1)),
        portfolio=dict(n_selected=len(sel), gross=float(tw.sum()), port_vol_ann=pv, k=k,
                       n_uptrend=len(cand)),
        stocks=stocks,
        dates=[x.strftime("%Y-%m-%d") for x in recent],
        closes={s: [None if not np.isfinite(v) else float(v) for v in C[s].reindex(recent).values] for s in UNIVERSE},
    )
    outdir.mkdir(exist_ok=True)
    (outdir / "latest.json").write_text(json.dumps(clean(latest), separators=(",", ":")))
    print(f"latest: asof {latest['asof']} next {latest['next_session']} rebalance_today={rebalance_today} "
          f"next_rebal_close={latest['next_rebalance_close']} selected={[UNIVERSE[j] for j in sel]} "
          f"gross={tw.sum():.2f} port_vol={pv:.3f} ihsg_above={latest['regime']['above']}")
    if not do_backtest:
        return

    # ---------------- walk-forward backtest
    with ProcessPoolExecutor(workers) as ex:
        wf = list(ex.map(walkforward, [(s, rpct[s], years) for s in UNIVERSE]))
    F = pd.DataFrame({o["name"]: o["f"] for o in wf}).reindex(cal).reindex(columns=UNIVERSE)
    MU = pd.DataFrame({o["name"]: o["mu"] for o in wf}).reindex(cal).reindex(columns=UNIVERSE)
    F = F.ffill(limit=10).where(A.notna())  # suspended days keep the last forecast
    R = pd.DataFrame(rpct).reindex(cal)
    Z = ((R - MU) / np.sqrt(F.shift(1))).values
    sig = (np.sqrt(F) / 100).values
    elig = ((A > sma) & (mom > 0) & (hist >= P["min_hist"]) & (liq >= P["liq_min_value"])
            & trade & F.notna()).values
    score = np.where(elig, mom.values / (sig * math.sqrt(P["dpy"])), np.nan)
    Av, AOv, Cv, Ov, Tv, smav = A.values, d["AO"].values, C.values, d["O"].values, trade.values, sma.values
    start_i = int(cal.searchsorted(pd.Timestamp(P["bt_start"])))
    cash, sh, peak = float(P["start_capital"]), np.zeros(n), np.full(n, np.nan)
    pending: dict[int, tuple[float, str]] = {}
    eq, gross_l, nh = [], [], []
    traded = costs = 0.0
    ntr = blocked_buy = blocked_sell = 0
    by_reason: dict[str, list[float]] = {}

    def book(why, value):
        r_ = by_reason.setdefault(why, [0, 0.0])
        r_[0] += 1
        r_[1] += value
    for i in range(start_i, len(cal)):
        if pending:
            carry = {}
            for j, (q, why) in list(pending.items()):
                if q >= 0:
                    continue
                ref = Cv[i - 1, j]
                _, b = ar_pcts(reg_idx[i], ref)
                if not Tv[i, j] or not np.isfinite(AOv[i, j]) or Ov[i, j] / ref - 1 <= -(b - 0.01):
                    carry[j] = (q, why)
                    blocked_sell += 1
                    continue
                qty = min(-q, sh[j])
                px = AOv[i, j] * (1 - P["slippage"])
                fee = qty * px * P["sell_fee"]
                cash += qty * px - fee
                sh[j] -= qty
                traded += qty * px
                book(why, qty * px)
                costs += fee + qty * AOv[i, j] * P["slippage"]
                ntr += 1
                if sh[j] <= 0:
                    sh[j], peak[j] = 0.0, np.nan
            buys = []
            whys = {}
            for j, (q, why) in pending.items():
                if q <= 0:
                    continue
                ref = Cv[i - 1, j]
                a, _ = ar_pcts(reg_idx[i], ref)
                if not Tv[i, j] or not np.isfinite(AOv[i, j]) or Ov[i, j] / ref - 1 >= a - 0.01:
                    blocked_buy += 1
                    continue
                buys.append((j, q))
                whys[j] = why
            need = sum(q * AOv[i, j] * (1 + P["slippage"]) * (1 + P["buy_fee"]) for j, q in buys)
            scale = min(1.0, cash / need) if need > 0 else 1.0
            for j, q in buys:
                qty = math.floor(q * scale / 100) * 100
                if qty <= 0:
                    continue
                px = AOv[i, j] * (1 + P["slippage"])
                fee = qty * px * P["buy_fee"]
                cash -= qty * px + fee
                sh[j] += qty
                traded += qty * px
                book(whys[j], qty * px)
                costs += fee + qty * AOv[i, j] * P["slippage"]
                ntr += 1
            pending = carry
        pos = np.where(sh > 0, sh * np.nan_to_num(Av[i]), 0.0)
        equity = cash + pos.sum()
        eq.append(equity)
        gross_l.append(pos.sum() / equity)
        nh.append(int((sh > 0).sum()))
        held = sh > 0
        peak = np.where(held, np.fmax(peak, Av[i]), np.nan)
        stopped = set()
        for j in np.where(held)[0]:
            s_ = sig[i, j]
            trend_break = Av[i, j] < smav[i, j]
            vol_stop = np.isfinite(s_) and Av[i, j] < peak[j] * (1 - P["stop_k"] * s_ * math.sqrt(P["stop_days"]))
            if trend_break or vol_stop:
                pending.setdefault(j, (-sh[j], "trend exit (below 200-day)" if trend_break else "volatility stop"))
                stopped.add(j)
        last_of_week = i == len(cal) - 1 or wk[i] != wk[i + 1]
        if last_of_week and i < len(cal) - 1:
            cand = [j for j in range(n) if elig[i, j] and j not in stopped]
            sel_b = select(sorted(cand, key=lambda j: -score[i, j]), {j for j in range(n) if sh[j] > 0})
            tgt_w = np.zeros(n)
            if sel_b:
                wb, _, _ = target_weights(sel_b, sig[i, sel_b], Z[max(0, i - P["corr_len"] + 1): i + 1, sel_b], float(ihsg_cap[i]))
                tgt_w[sel_b] = wb
            ref_prev = Cv[i - 1]
            for j in range(n):
                if j in pending:
                    continue
                tw_, cw_ = tgt_w[j], pos[j] / equity
                if tw_ == 0 and cw_ == 0:
                    continue
                if tw_ > 0 and cw_ > 0 and abs(tw_ - cw_) < max(P["band"], P["resize_frac"] * tw_):
                    continue
                tgt = math.floor(tw_ * equity / Av[i, j] / 100) * 100 if tw_ > 0 else 0
                q = tgt - sh[j]
                if q == 0:
                    continue
                if q > 0:
                    a, _ = ar_pcts(reg_idx[i], ref_prev[j])
                    if Cv[i, j] / ref_prev[j] - 1 >= a - 0.01:  # closed locked at ARA: don't chase
                        continue
                why = "new position" if sh[j] == 0 else ("dropped at rebalance" if tgt == 0 else "resize")
                pending[j] = (q, why)

    idx = cal[start_i:]
    eqs = pd.Series(eq, index=idx) / P["start_capital"] * 100
    ew_ret = d["ret"].loc[idx].where(trade.loc[idx]).mean(axis=1).fillna(0)
    ew = (1 + ew_ret).cumprod()
    ew = ew / ew.iloc[0] * 100
    ih = d["ihsg"].loc[idx] / d["ihsg"].loc[idx].iloc[0] * 100
    yrs = len(idx) / P["dpy"]
    yearly = []
    for y in sorted(set(idx.year)):
        m = idx.year == y
        def yr(s):
            s_ = s[m]
            prev = s[idx < pd.Timestamp(y, 1, 1)]
            base = prev.iloc[-1] if len(prev) else s_.iloc[0]
            return float(s_.iloc[-1] / base - 1)
        yearly.append(dict(year=int(y), strategy=yr(eqs), ew=yr(ew), ihsg=yr(ih)))
    # VaR check of the IHSG model: out-of-sample 1% one-day VaR exceedances
    f_i, mu_i, q_i = wf_ihsg["f"], wf_ihsg["mu"], wf_ihsg["q"]
    thr = (mu_i + np.sqrt(f_i) * q_i).shift(1)
    both = pd.concat([ihsg_r.reindex(thr.index), thr], axis=1).dropna()
    hits = (both.iloc[:, 0] < both.iloc[:, 1]).values
    avg_eq = float(np.mean(eq))
    models_used = {}
    for o in wf:
        for v in o["models"].values():
            models_used[v] = models_used.get(v, 0) + 1
    # Alpha/beta of the strategy versus the IHSG (daily OLS, Newey-West t-stat with 5 lags):
    # separates "we held the index" from "we added something on top of it".
    rs, rm = eqs.pct_change().dropna(), ih.pct_change().dropna()
    both_ = pd.concat([rs, rm], axis=1).dropna().values
    Xm = np.column_stack([np.ones(len(both_)), both_[:, 1]])
    beta_hat = np.linalg.lstsq(Xm, both_[:, 0], rcond=None)[0]
    resid = both_[:, 0] - Xm @ beta_hat
    XtX_inv = np.linalg.inv(Xm.T @ Xm)
    S = (Xm * resid[:, None]).T @ (Xm * resid[:, None])
    for lag in range(1, 6):
        w = 1 - lag / 6
        G = (Xm[lag:] * resid[lag:, None]).T @ (Xm[:-lag] * resid[:-lag, None])
        S += w * (G + G.T)
    se = np.sqrt(np.diag(XtX_inv @ S @ XtX_inv))
    alpha_beta = dict(alpha_ann=float(beta_hat[0] * P["dpy"]), beta=float(beta_hat[1]),
                      t_alpha=float(beta_hat[0] / se[0]) if se[0] > 0 else None,
                      r2=float(1 - resid.var() / both_[:, 0].var()) if both_[:, 0].var() > 0 else None)
    bt = dict(
        version=VERSION, generated_utc=latest["generated_utc"], alpha_beta=alpha_beta,
        start=idx[0].strftime("%Y-%m-%d"), end=idx[-1].strftime("%Y-%m-%d"),
        dates=[x.strftime("%Y-%m-%d") for x in idx],
        strategy=[round(v, 2) for v in eqs.values], ew=[round(v, 2) for v in ew.values],
        ihsg=[round(v, 2) for v in ih.values],
        exposure=[round(v, 3) for v in gross_l],
        stats=dict(strategy=perf(eqs), ew=perf(ew), ihsg=perf(ih)),
        yearly=yearly,
        trading=dict(avg_holdings=float(np.mean(nh)), avg_exposure=float(np.mean(gross_l)),
                     turnover_pa=traded / avg_eq / yrs, costs_pa=costs / avg_eq / yrs,
                     trades=ntr, trades_pa=ntr / yrs, blocked_buys=blocked_buy, blocked_sells=blocked_sell,
                     by_reason={k_: dict(trades_pa=v[0] / yrs, turnover_pa=v[1] / avg_eq / yrs)
                                for k_, v in sorted(by_reason.items(), key=lambda kv: -kv[1][1])}),
        var_test=kupiec(hits), clipped_days=d["clipped"], models_used=models_used,
        start_capital=P["start_capital"],
    )
    (outdir / "backtest.json").write_text(json.dumps(clean(bt), separators=(",", ":")))
    s = bt["stats"]
    print("backtest", bt["start"], "->", bt["end"])
    for k_ in ("strategy", "ew", "ihsg"):
        print(f"  {k_:9s} CAGR {s[k_]['cagr']:+.2%} vol {s[k_]['vol']:.2%} Sharpe {s[k_]['sharpe']:.2f} "
              f"maxDD {s[k_]['max_dd']:.2%} PSR {s[k_]['psr']:.2f}")
    t_ = bt["trading"]
    print(f"  holdings {t_['avg_holdings']:.1f} exposure {t_['avg_exposure']:.0%} turnover {t_['turnover_pa']:.1f}x/yr "
          f"costs {t_['costs_pa']:.2%}/yr trades {t_['trades_pa']:.0f}/yr blocked buys {blocked_buy} sells {blocked_sell}")
    for k_, v in bt["trading"]["by_reason"].items():
        print(f"    {k_:28s} {v['trades_pa']:6.1f} trades/yr  turnover {v['turnover_pa']:.2f}x/yr")
    ab = bt["alpha_beta"]
    print(f"  vs IHSG: beta {ab['beta']:.2f}, alpha {ab['alpha_ann']:+.2%}/yr (t = {ab['t_alpha']:.2f}), R2 {ab['r2']:.2f}")
    print("  VaR test", bt["var_test"], "clipped", d["clipped"], "models", models_used)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backtest", action="store_true", help="also run the walk-forward backtest")
    ap.add_argument("--cache", help="read raw prices from a pickle instead of downloading")
    ap.add_argument("--out", default="out")
    ap.add_argument("--v2", action="store_true", help="trial-2 rules: keep while in top 16, resize only if >50% off target")
    ap.add_argument("--broker", choices=sorted(FEES), help="fee profile (default ipot)")
    ap.add_argument("--holdings", help="comma-separated symbols currently held (v2 retention in the live snapshot)")
    a = ap.parse_args()
    configure(a.v2, a.broker)
    raw = pd.read_pickle(a.cache) if a.cache else download()
    run(raw, a.backtest, Path(a.out), [x.strip().upper() for x in (a.holdings or "").split(",") if x.strip()])


if __name__ == "__main__":
    main()
