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
import io
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

VERSION = "3.0"
UNIVERSE = (
    "BBCA BBRI BMRI BBNI BRIS TLKM ISAT EXCL TOWR ASII UNTR UNVR ICBP INDF MYOR "
    "KLBF CPIN JPFA AMRT MAPI ACES ADRO PTBA ITMG MEDC PGAS AKRA ANTM INCO SMGR"
).split()
INDEX = "^JKSE"
FX = "IDR=X"  # USD/IDR on Yahoo Finance
FIXED30 = None            # filled below: the hand-picked 30 (hindsight universe, production)
UNIVERSE_MODE = "fixed30" # or "lq45": point-in-time LQ45 membership from docs/data/lq45_history.csv
LQ45_HISTORY = Path(__file__).resolve().parent.parent / "docs" / "data" / "lq45_history.csv"
MEMBERSHIP: list[tuple] = []  # [(start, end, set(tickers))] when UNIVERSE_MODE == "lq45"
# Names to keep out of the universe even if they pass the filters: BEI's HSC (high shareholding concentration) list,
# UMA notices and special-notation stocks. Maintained by hand; none of the 30 are on the October 2026 HSC list.
EXCLUDE: set[str] = set()
# IDX-SVI composite stress gauge (v3): component weights, renormalised over whatever is available that day
SVI_W = dict(rv20=0.20, garch=0.15, vxeem=0.15, flow=0.15, breadth=0.10, idr=0.10, wvf=0.10)
SVI_LABELS = dict(rv20="Basket realised volatility, 20 days", garch="IHSG GARCH forecast volatility",
                  vxeem="Emerging-market implied volatility (Cboe VXEEM)",
                  flow="Big banks behind the basket, 5 days (foreign-flow proxy)",
                  breadth="Share of stocks down more than 3% or limit-locked, 5 days",
                  idr="USD/IDR realised volatility, 20 days", wvf="Williams VIX Fix on the basket")
TIER_LABELS = ("normal", "elevated", "risk-off")
# Event windows: MSCI quarterly index-review announcements (approximate: the 11th of Feb/May/Aug/Nov) plus the
# Indonesia classification dates known in October 2026. Replace with exact dates when MSCI publishes them.
EVENTS = [f"{y}-{m}-11" for y in range(2016, 2028) for m in ("02", "05", "08", "11")] + ["2026-11-30", "2027-04-30", "2027-06-30"]
FIXED30 = list(UNIVERSE)


def set_universe(mode: str, raw: pd.DataFrame | None = None) -> None:
    """fixed30 = the hand-picked list. lq45 = every name that was in LQ45 during the history file, with
    membership applied date by date in the backtest (no hindsight), restricted to names Yahoo still serves."""
    global UNIVERSE, UNIVERSE_MODE, MEMBERSHIP
    UNIVERSE_MODE = mode
    if mode == "fixed30":
        UNIVERSE, MEMBERSHIP = list(FIXED30), []
        return
    hist = pd.read_csv(LQ45_HISTORY)
    MEMBERSHIP = [(pd.Timestamp(r.start), pd.Timestamp(r.end), set(r.tickers.split())) for r in hist.itertuples()]
    names = sorted(set().union(*[m[2] for m in MEMBERSHIP]))
    if raw is not None:
        names = [t for t in names if (t + ".JK") in raw["Close"].columns and raw["Close"][t + ".JK"].notna().sum() > 300]
    UNIVERSE = names


def member_mask(cal: pd.DatetimeIndex) -> pd.DataFrame:
    """True where a name was an LQ45 constituent on that date (latest announcement whose start <= date)."""
    if not MEMBERSHIP:
        return pd.DataFrame(True, index=cal, columns=UNIVERSE)
    starts = np.array([m[0].value for m in MEMBERSHIP])
    order = np.argsort(starts)
    out = np.zeros((len(cal), len(UNIVERSE)), bool)
    col = {t: j for j, t in enumerate(UNIVERSE)}
    for i, day in enumerate(cal):
        k = np.searchsorted(starts[order], day.value, side="right") - 1
        if k < 0:
            continue
        for t in MEMBERSHIP[order[k]][2]:
            if t in col:
                out[i, col[t]] = True
    return pd.DataFrame(out, index=cal, columns=UNIVERSE)


P = dict(
    n_hold=8, max_w=0.20, vol_target=0.15, sma_len=200, mom_len=126,
    stop_k=2.5, stop_days=5, band=0.02, bear_cap=0.50, dpy=240,
    min_hist=252, corr_len=252, liq_len=60, liq_min_value=10e9,
    buy_fee=0.0019, sell_fee=0.0029, slippage=0.001,
    buy_buffer=0.01, sell_buffer=0.02,
    start_capital=100_000_000, bt_start="2016-01-01",
    # v2 (Oct 2026): hysteresis and coarser resizing to cut turnover; brake on the bot's own basket;
    # volatility floor from 2-day returns while IDX's lower limit (ARB) is 10% or tighter (censored days)
    n_keep=16, resize_frac=0.5, hysteresis=True, brake_src="basket", floor_arb=0.10, rv2_len=40,
    # v3 (Oct 2026): three-tier stress gauge on top of the trend brake, and MSCI event windows
    svi=True, svi_lo=0.5, svi_hi=1.5, tier_caps=(1.0, 0.7, 0.3), tier_vol=(0.15, 0.10, 0.10),
    event_cap=0.5, event_days=3, z_len=252,
)


def configure(mode: str) -> None:
    """mode 'v1' = the original rulebook (top-8 rotation, 2pp band) with the same data fixes; 'v2' = default."""
    global VERSION
    if mode == "v3":
        P.update(svi=True)
        VERSION = "3.0"
    if mode == "v1":
        P.update(hysteresis=False, resize_frac=0.0, svi=False)
        VERSION = "1.1"
    elif mode == "v2":
        P.update(svi=False)
        VERSION = "2.0"


def select(order: list[int], held: list[int], allow_new: bool = True) -> list[int]:
    """v1: the top n_hold. v2: keep holdings that are still eligible and inside the top n_keep,
    then fill the remaining slots from the top n_hold in rank order. v3: no new names while stress is elevated."""
    if not P["hysteresis"]:
        return order[: P["n_hold"]] if allow_new else [j for j in order[: P["n_hold"]] if j in held]
    top_keep = set(order[: P["n_keep"]])
    sel = [j for j in order if j in held and j in top_keep][: P["n_hold"]]
    if not allow_new:
        return sel
    for j in order[: P["n_hold"]]:
        if len(sel) >= P["n_hold"]:
            break
        if j not in sel:
            sel.append(j)
    return sel
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
    ("2026-10-19", (0.35, 0.25, 0.20), (0.07, 0.07, 0.07)),  # Kep-00157/BEI/10-2026, to 31 Dec 2026
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

    names = UNIVERSE if UNIVERSE_MODE == "fixed30" else sorted(set(FIXED30) | set().union(*[m[2] for m in MEMBERSHIP]))
    tickers = [s + ".JK" for s in names] + [INDEX, FX]
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


def target_weights(sel, sd, Zwin, cap, vol_target=None):
    """Inverse-vol weights, 20% cap, scaled to the vol target; gross <= cap."""
    vt = vol_target or P["vol_target"]
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
    k = min(cap / w.sum(), (vt / math.sqrt(P["dpy"])) / pv)
    return w * k, pv * math.sqrt(P["dpy"]), k


# ------------------------------------------------------------- stress gauge (IDX-SVI)


def fred_series(sid: str, cache: Path | None = None) -> pd.Series | None:
    """Daily series from FRED (free, no key). Falls back to the cached copy if the download fails."""
    try:
        import requests

        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "value"]
        s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values, index=pd.to_datetime(df["date"])).dropna()
        if cache is not None:
            s.to_csv(cache, header=["value"])
        return s
    except Exception as e:
        if cache is not None and cache.exists():
            return pd.read_csv(cache, index_col=0, parse_dates=True)["value"]
        print(f"FRED {sid} unavailable: {e}")
        return None


def zs(x: pd.Series) -> pd.Series:
    """Rolling z-score over the past z_len days (no look-ahead), winsorised at +/-3."""
    m = x.rolling(P["z_len"], min_periods=126).mean()
    sd = x.rolling(P["z_len"], min_periods=126).std()
    return ((x - m) / sd).clip(-3, 3)


def event_flags(cal: pd.DatetimeIndex) -> np.ndarray:
    ev = np.zeros(len(cal), bool)
    for dstr in EVENTS:
        day = pd.Timestamp(dstr)
        pos = int(cal.searchsorted(day))
        if pos >= len(cal):  # event after the last session: flag the tail only if it is within event_days
            gap = int(np.busday_count(cal[-1].date(), day.date()))
            if gap <= P["event_days"]:
                ev[len(cal) - (P["event_days"] - gap + 1):] = True
            continue
        ev[max(0, pos - P["event_days"]): pos + P["event_days"] + 1] = True
    return ev


def svi(d: dict, raw: pd.DataFrame, cal: pd.DatetimeIndex, trade: pd.DataFrame, basket: pd.Series,
        f_ihsg: pd.Series, locked: np.ndarray, var1_ihsg: float, outdir: Path) -> dict:
    """Composite stress gauge: weighted average of z-scored components, each known at that day's close."""
    ret = d["ret"].where(trade)
    br = basket.pct_change()
    ntr = trade.sum(axis=1).clip(lower=1)
    comp: dict[str, pd.Series] = {}
    comp["rv20"] = br.rolling(20).std() * math.sqrt(P["dpy"])
    f = f_ihsg.reindex(cal).astype(float)
    if np.isfinite(var1_ihsg):
        f.iloc[-1] = var1_ihsg
    comp["garch"] = np.sqrt(f) / 100 * math.sqrt(P["dpy"])
    vx = fred_series("VXEEMCLS", outdir / "vxeem.csv")
    if vx is not None:
        comp["vxeem"] = vx.reindex(cal, method="ffill").shift(1)  # US close is after the IDX close: use yesterday's
    if "Close" in raw and FX in raw["Close"].columns:
        fx = raw["Close"][FX].reindex(cal).ffill()
        comp["idr"] = np.log(fx).diff().rolling(20).std() * math.sqrt(P["dpy"])
    down = (ret < -0.03).sum(axis=1) / ntr
    lock = pd.Series(locked.sum(axis=1), index=cal) / ntr
    comp["breadth"] = (down + lock).rolling(5).mean()
    banks = [b for b in ("BBCA", "BMRI", "BBRI") if b in UNIVERSE]
    b5 = np.expm1(np.log1p(ret[banks].mean(axis=1).fillna(0)).rolling(5).sum())
    k5 = np.expm1(np.log1p(br.fillna(0)).rolling(5).sum())
    comp["flow"] = -(b5 - k5)
    hi22 = basket.rolling(22).max()
    comp["wvf"] = (hi22 - basket) / hi22 * 100
    Z = {}
    for k, v in comp.items():
        Z[k] = (zs(v) + zs(v - v.shift(5))) / 2 if k == "vxeem" else zs(v)
    Z = pd.DataFrame(Z)
    W = pd.Series({k: SVI_W[k] for k in Z.columns})
    val = (Z.fillna(0) * W).sum(axis=1) / (Z.notna() * W).sum(axis=1).replace(0, np.nan)
    tier = pd.Series(np.where(val >= P["svi_hi"], 2, np.where(val >= P["svi_lo"], 1, 0)), index=cal)
    tier[val.isna()] = 0
    ev = event_flags(cal)
    cap = np.minimum(np.array([P["tier_caps"][t] for t in tier]), np.where(ev, P["event_cap"], 1.0))
    vt = np.array([P["tier_vol"][t] for t in tier])
    return dict(value=val, tier=tier, cap=pd.Series(cap, index=cal), vol_target=pd.Series(vt, index=cal),
                event=pd.Series(ev, index=cal), Z=Z, comp=pd.DataFrame(comp), weights=W)


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


def update_trials(path: Path, version: str, sharpe: float) -> list[dict]:
    """Record every strategy variant ever backtested on this data, so the Deflated Sharpe Ratio can count them."""
    tr = json.loads(path.read_text()) if path.exists() else []
    tr = [t for t in tr if t["version"] != version] + [dict(version=version, sharpe=round(float(sharpe), 4),
                                                            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))]
    path.write_text(json.dumps(tr, indent=1))
    return tr


def dsr(eq: pd.Series, sharpes_ann: list[float]) -> float | None:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014): PSR against the Sharpe expected from the best of
    N trials that all had zero true skill. sharpes_ann = annualised Sharpe of every trial, including this one."""
    from scipy.stats import kurtosis, norm, skew

    if len(sharpes_ann) < 2:
        return None
    r = eq.pct_change().dropna()
    n, root = len(r), math.sqrt(P["dpy"])
    srs = np.array(sharpes_ann) / root
    sr = float(r.mean() / r.std(ddof=1))
    v = float(np.var(srs, ddof=1))
    if v <= 0:
        return None
    N, g = len(srs), 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * norm.ppf(1 - 1 / N) + g * norm.ppf(1 - 1 / (N * math.e)))
    g3, g4 = float(skew(r)), float(kurtosis(r, fisher=False))
    return float(norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr**2, 1e-9))))


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


def run(raw: pd.DataFrame, do_backtest: bool, outdir: Path, held: list[str] | None = None,
        suffix: str = "", trials_path: Path | None = None) -> None:
    if UNIVERSE_MODE == "lq45":
        set_universe("lq45", raw)
    d = prepare(raw)
    held_idx = [UNIVERSE.index(h) for h in (held or []) if h in UNIVERSE]
    cal, A, C, trade = d["cal"], d["A"], d["C"], d["trade"]
    n = len(UNIVERSE)
    rpct = {s: 100 * np.log1p(d["ret"][s].where(trade[s])) for s in UNIVERSE}
    ihsg_r = 100 * np.log(d["ihsg"] / d["ihsg"].shift(1))
    years = list(range(int(P["bt_start"][:4]), cal[-1].year + 1))
    workers = max(1, min(8, (os.cpu_count() or 2)))

    sma = A.rolling(P["sma_len"], min_periods=P["sma_len"]).mean()
    mom = A / A.shift(P["mom_len"]) - 1
    hist = A.notna().cumsum()
    liq = d["value"].rolling(P["liq_len"], min_periods=20).median()
    ihsg_sma = d["ihsg"].rolling(200, min_periods=200).mean()
    # market brake: the bot's own equal-weight basket (self-contained; IHSG's composition changes in Oct 2026)
    member = member_mask(cal)
    basket = (1 + d["ret"].where(trade & member).mean(axis=1).fillna(0)).cumprod()
    basket_sma = basket.rolling(200, min_periods=200).mean()
    brake, brake_sma = (basket, basket_sma) if P["brake_src"] == "basket" else (d["ihsg"], ihsg_sma)
    ihsg_cap = np.where(brake > brake_sma, 1.0, P["bear_cap"])
    # censoring: while ARB <= floor_arb the daily range is cut, so floor the vol at the 2-day realised vol
    rv2 = np.log(A / A.shift(2)).rolling(P["rv2_len"], min_periods=20).std() / math.sqrt(2)
    rv2v = rv2.values
    iso = cal.isocalendar()
    wk = list(zip(iso["year"], iso["week"]))
    rc = C / C.shift(1) - 1  # close-to-close move, used to spot limit locks
    reg_idx = np.searchsorted(AR_DATES, cal.values, side="right") - 1
    cens = np.array([AR_REGIMES[k][2][1] <= P["floor_arb"] for k in reg_idx])
    refv = np.nan_to_num(C.shift(1).values, nan=1000.0)
    bandv = np.where(refv <= 200, 0, np.where(refv <= 5000, 1, 2))
    ara_t = np.take_along_axis(np.array([AR_REGIMES[k][1] for k in reg_idx]), bandv, axis=1)
    arb_t = np.take_along_axis(np.array([AR_REGIMES[k][2] for k in reg_idx]), bandv, axis=1)
    rcv = rc.values
    locked = ((rcv >= ara_t - 0.01) | (rcv <= -(arb_t - 0.01))) & trade.values & np.isfinite(rcv)
    locked60 = pd.DataFrame(locked, index=cal, columns=UNIVERSE).rolling(60, min_periods=1).sum()

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
    wf_ihsg = walkforward(("IHSG", ihsg_r, years))
    sv = svi(d, raw, cal, trade, basket, wf_ihsg["f"], locked, L["IHSG"]["var1"], outdir)
    cap_all = np.minimum(ihsg_cap, sv["cap"].values) if P["svi"] else ihsg_cap
    vt_all = sv["vol_target"].values if P["svi"] else np.full(len(cal), P["vol_target"])
    allow_new_all = (sv["tier"].values < 1) if P["svi"] else np.ones(len(cal), bool)
    not_excluded = np.array([s_ not in EXCLUDE for s_ in UNIVERSE])
    sd_live = np.array([math.sqrt(L[s]["var1"]) / 100 if np.isfinite(L[s]["var1"]) else np.nan for s in UNIVERSE])
    sd_garch = sd_live.copy()
    if cens[i]:
        ok = np.isfinite(sd_live) & np.isfinite(rv2v[i])
        sd_live = np.where(ok, np.maximum(sd_live, rv2v[i]), sd_live)
    Zl = pd.DataFrame({s: L[s]["z"] for s in UNIVERSE}).reindex(cal)
    elig_live = ((A.iloc[i] > sma.iloc[i]) & (mom.iloc[i] > 0) & (hist.iloc[i] >= P["min_hist"])
                 & (liq.iloc[i] >= P["liq_min_value"]) & trade.iloc[i] & member.iloc[i]).values & np.isfinite(sd_live) & not_excluded
    score_live = np.where(elig_live, mom.iloc[i].values / (sd_live * math.sqrt(P["dpy"])), np.nan)
    cand = [j for j in range(n) if elig_live[j]]
    order = sorted(cand, key=lambda j: -score_live[j])
    sel = select(order, held_idx, bool(allow_new_all[i]))
    w, pv, k = target_weights(sel, sd_live[sel], Zl.iloc[-P["corr_len"]:, sel].values, float(cap_all[i]), float(vt_all[i]))
    tw = np.zeros(n)
    tw[sel] = w
    t_ = int(sv["tier"].iloc[i])
    sv_info = dict(
        value=float(sv["value"].iloc[i]) if np.isfinite(sv["value"].iloc[i]) else None, tier=t_, label=TIER_LABELS[t_],
        cap=float(sv["cap"].iloc[i]), vol_target=float(sv["vol_target"].iloc[i]), event_window=bool(sv["event"].iloc[i]),
        applied=bool(P["svi"]), thresholds=[P["svi_lo"], P["svi_hi"]],
        components=[dict(name=k_, label=SVI_LABELS[k_], weight=float(sv["weights"][k_]),
                         value=float(sv["comp"][k_].iloc[i]) if np.isfinite(sv["comp"][k_].iloc[i]) else None,
                         z=float(sv["Z"][k_].iloc[i]) if np.isfinite(sv["Z"][k_].iloc[i]) else None) for k_ in sv["Z"].columns],
        history=dict(dates=[x.strftime("%Y-%m-%d") for x in cal[-300:]],
                     values=[None if not np.isfinite(v) else round(float(v), 2) for v in sv["value"].iloc[-300:]],
                     tiers=[int(t) for t in sv["tier"].iloc[-300:]]),
    )
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
    hist5 = np.sqrt(wf_ihsg["f"].dropna().iloc[-5 * P["dpy"]:]) / 100 * math.sqrt(P["dpy"])
    ihsg_vol = math.sqrt(ihsg_var) / 100 * math.sqrt(P["dpy"])
    stocks = []
    for j, s in enumerate(UNIVERSE):
        if s in EXCLUDE:
            status = "Excluded: on the HSC / UMA watch list"
        elif not trade[s].iloc[i]:
            status = "Not traded today (suspended or no data)"
        elif not bool(member.iloc[i][s]):
            status = "Not an LQ45 member this period"
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
        elif j in sel and j in held_idx and order.index(j) >= P["n_hold"]:
            status = f"Held, ranked {order.index(j) + 1}: kept while inside the top {P['n_keep']}"
        elif j in sel:
            status = f"Selected (rank {order.index(j) + 1})"
        elif j in held_idx:
            status = f"Held, ranked {order.index(j) + 1}: outside the top {P['n_keep']}, exit at rebalance"
        else:
            status = f"Uptrend, ranked {order.index(j) + 1}: outside the top {P['n_hold']}"
        stocks.append(dict(
            sym=s, close=C[s].iloc[i], prev_close=C[s].iloc[i - 1], sma200_ok=bool(A.iloc[i][s] > sma.iloc[i][s]),
            mom126=mom.iloc[i][s], sigd=sd_live[j], siga=sd_live[j] * math.sqrt(P["dpy"]),
            score=score_live[j], eligible=bool(elig_live[j]), selected=j in sel,
            rank=order.index(j) + 1 if j in order else None, target_w=tw[j], status=status,
            ara_locked=bool(ara_l[j]), arb_locked=bool(arb_l[j]),
            stop_mult=1 - P["stop_k"] * sd_live[j] * math.sqrt(P["stop_days"]) if np.isfinite(sd_live[j]) else None,
            liq_bn=liq.iloc[i][s] / 1e9, model=L[s]["model"], held=j in held_idx,
            sigd_garch=sd_garch[j], vol_floor=bool(cens[i] and np.isfinite(sd_live[j]) and sd_live[j] > sd_garch[j] + 1e-12),
            locked_60d=int(locked60.iloc[i][s]) if np.isfinite(locked60.iloc[i][s]) else 0,
        ))
    recent = cal[-300:]
    latest = dict(
        version=VERSION, generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        asof=asof.strftime("%Y-%m-%d"), next_session=nxt.strftime("%Y-%m-%d"),
        rebalance_today=bool(rebalance_today), next_rebalance_close=d2.strftime("%Y-%m-%d"),
        params=P, universe=UNIVERSE,
        svi=sv_info,
        rules=dict(version=VERSION, universe=UNIVERSE_MODE, n_names=len(UNIVERSE), svi=P["svi"], hysteresis=P["hysteresis"], n_keep=P["n_keep"], resize_frac=P["resize_frac"],
                   brake_src=P["brake_src"], censored_regime=bool(cens[i]), arb_pct=float(AR_REGIMES[reg_idx[i]][2][1])),
        regime=dict(ihsg_close=d["ihsg"].iloc[i], ihsg_sma200=ihsg_sma.iloc[i],
                    above=bool(brake.iloc[i] > brake_sma.iloc[i]), cap=float(cap_all[i]), trend_cap=float(ihsg_cap[i]),
                    ihsg_above=bool(d["ihsg"].iloc[i] > ihsg_sma.iloc[i]),
                    basket=float(basket.iloc[i] * 100), basket_sma200=float(basket_sma.iloc[i] * 100) if np.isfinite(basket_sma.iloc[i]) else None,
                    basket_change=float(basket.iloc[i] / basket.iloc[i - 1] - 1),
                    pct_locked_today=float(locked[i].sum() / max(1, trade.iloc[i].sum())),
                    ihsg_vol_ann=ihsg_vol, ihsg_vol_pctile_5y=float((hist5 < ihsg_vol).mean()),
                    ihsg_change=float(d["ihsg"].iloc[i] / d["ihsg"].iloc[i - 1] - 1)),
        portfolio=dict(n_selected=len(sel), gross=float(tw.sum()), port_vol_ann=pv, k=k,
                       n_uptrend=len(cand)),
        stocks=stocks,
        dates=[x.strftime("%Y-%m-%d") for x in recent],
        closes={s: [None if not np.isfinite(v) else float(v) for v in C[s].reindex(recent).values] for s in UNIVERSE},
    )
    outdir.mkdir(exist_ok=True)
    (outdir / f"latest{suffix}.json").write_text(json.dumps(clean(latest), separators=(",", ":")))
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
    sig = np.where(cens[:, None] & np.isfinite(sig) & np.isfinite(rv2v), np.maximum(sig, rv2v), sig)
    elig = ((A > sma) & (mom > 0) & (hist >= P["min_hist"]) & (liq >= P["liq_min_value"])
            & trade & member & F.notna()).values & not_excluded[None, :]
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
            order_b = sorted(cand, key=lambda j: -score[i, j])
            sel_b = select(order_b, [j for j in range(n) if sh[j] > 0 and j not in stopped], bool(allow_new_all[i]))
            tgt_w = np.zeros(n)
            if sel_b:
                wb, _, _ = target_weights(sel_b, sig[i, sel_b], Z[max(0, i - P["corr_len"] + 1): i + 1, sel_b], float(cap_all[i]), float(vt_all[i]))
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
    trials = update_trials(trials_path or (outdir / "trials.json"), VERSION, perf(eqs)["sharpe"])
    bt = dict(
        version=VERSION, generated_utc=latest["generated_utc"], rules=latest["rules"],
        trials=trials, dsr=dsr(eqs, [t["sharpe"] for t in trials]),
        svi=[None if not np.isfinite(v) else round(float(v), 2) for v in sv["value"].loc[idx]],
        svi_tier_days={TIER_LABELS[t]: int((sv["tier"].loc[idx] == t).sum()) for t in range(3)},
        event_days=int(sv["event"].loc[idx].sum()),
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
    (outdir / f"backtest{suffix}.json").write_text(json.dumps(clean(bt), separators=(",", ":")))
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
    print("  VaR test", bt["var_test"], "clipped", d["clipped"], "models", models_used)
    print(f"  trials {len(trials)} -> DSR {bt['dsr']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backtest", action="store_true", help="also run the walk-forward backtest")
    ap.add_argument("--cache", help="read raw prices from a pickle instead of downloading")
    ap.add_argument("--out", default="out")
    ap.add_argument("--rules", choices=["v1", "v2", "v3"], default=os.environ.get("BOT_RULES", "v2"),
                    help="v1 = original rulebook with the data fixes; v2 = hysteresis (default, in production); "
                         "v3 = v2 + the stress gauge applied to orders (trial #3: did not beat v2, kept for display only)")
    ap.add_argument("--suffix", default="", help="suffix for the output files, e.g. _v1")
    ap.add_argument("--held", default="", help="comma-separated symbols currently held (for the v2 selection)")
    ap.add_argument("--universe", choices=["fixed30", "lq45"], default="fixed30",
                    help="fixed30 = production list; lq45 = point-in-time LQ45 membership (needs docs/data/lq45_history.csv)")
    ap.add_argument("--start", default=None, help="backtest start date, e.g. 2022-02-01")
    ap.add_argument("--trials", default=None, help="trials file for the DSR (default <out>/trials.json)")
    a = ap.parse_args()
    configure(a.rules)
    set_universe(a.universe)
    if a.start:
        P["bt_start"] = a.start
    raw = pd.read_pickle(a.cache) if a.cache else download()
    run(raw, a.backtest, Path(a.out), held=[h for h in a.held.upper().split(",") if h], suffix=a.suffix,
        trials_path=Path(a.trials) if a.trials else None)


if __name__ == "__main__":
    main()
