#!/usr/bin/env python3
"""Kronos layer for the IDX Trend Bot (v0.1, October 2026). Advisory only: it never places orders.

After each close (called by `tasks.eod`, or by hand):
  python -m bot.kronos_signal              -> docs/data/kronos.json + one line per name in docs/data/kronos_log.csv
  python -m bot.kronos_signal --eval 30    -> docs/data/kronos_eval.json: how good were the last 30 days of forecasts?

Kronos (github.com/shiyu-coder/Kronos, MIT, AAAI 2026) is an open foundation model for candlesticks. Given the
last 400 daily bars of a name it samples plausible paths for the next 5 sessions. We draw K paths per name and read:
  ret5_med        median 5-day return of the sampled paths (the model's central view)
  p_up5           share of paths that end above today's close (how one-sided the model's view is)
  ret5_p10 / p90  the model's own 10th-90th percentile range for the 5-day return
  kvol_d          dispersion of the sampled next-day returns = a model-implied daily volatility, compared with the
                  GJR-GARCH forecast the engine already uses (ratio > 1.3 or < 0.7 is flagged as a disagreement)
The engine's rules are unchanged. Kronos is logged forward (kronos_log.csv) so it can be judged honestly later.
Caveat: Kronos was pre-trained on data up to 2024/25, so backtests before that are contaminated; only forward data
and `--eval` on recent sessions (after the model's training window) count.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine

VERSION = "0.1"
MODEL = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
TOKENIZER = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
PATHS = int(os.environ.get("KRONOS_PATHS", "16"))      # sampled paths per name
LOOKBACK = int(os.environ.get("KRONOS_LOOKBACK", "400"))  # daily bars of context (model maximum 512)
HORIZON = 5                                             # sessions ahead
CHUNK = int(os.environ.get("KRONOS_CHUNK", "64"))       # sequences per forward pass (memory)
TEMP, TOP_P = 1.0, 0.9
VOL_HI, VOL_LO = 1.3, 0.7                               # Kronos vol / GARCH vol disagreement flags
NAMES_ALL = ["IHSG"] + engine.UNIVERSE


# ------------------------------------------------------------------ data


def bars(raw: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Daily OHLCV for one name, dividend/split adjusted, suspended days removed, data errors neutralised."""
    tk = engine.INDEX if sym == "IHSG" else sym + ".JK"
    cols = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj", "Volume": "volume"}
    df = pd.DataFrame({v: raw[k][tk] for k, v in cols.items()})
    df = df[df.index.dayofweek < 5].dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    if sym != "IHSG":
        df = df[df["volume"] > 0]  # not traded that day
    factor = (df["adj"] / df["close"]).replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(1.0)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * factor
    # IDX cannot move a stock more than 35% in a session: bigger jumps are data errors -> repeat the previous bar
    bad = (df["close"] / df["close"].shift(1) - 1).abs() > 0.35
    for c in ("open", "high", "low", "close"):
        df.loc[bad, c] = np.nan
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].ffill()
    df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    df = df.drop(columns="adj")
    return df


def future_sessions(last: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    out, d = [], pd.Timestamp(last)
    for _ in range(n):
        d = engine.next_session(d)
        out.append(d)
    return out


# ------------------------------------------------------------------ model


def load_predictor():
    """Load Kronos from the Hugging Face hub (cached after the first run). Returns None if torch is missing."""
    try:
        import torch
        from .kronos import Kronos, KronosPredictor, KronosTokenizer
    except Exception as e:  # torch / einops not installed, or no network for the first download
        print(f"Kronos unavailable: {e}")
        return None
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    tok = KronosTokenizer.from_pretrained(TOKENIZER)
    mdl = Kronos.from_pretrained(MODEL)
    tok.eval(), mdl.eval()
    return KronosPredictor(mdl, tok, device="cpu", max_context=512)


def sample_paths(pred, windows: dict[str, pd.DataFrame], k: int = PATHS, horizon: int = HORIZON,
                 seed: int | None = None) -> dict[str, np.ndarray]:
    """For each name draw k independent paths. Returns {name: array (k, horizon, 6)} = open high low close vol amt.

    Kronos averages its own samples when sample_count > 1, so to keep the spread we submit k copies of each
    window as separate series (sample_count=1 each) through predict_batch, chunked to limit memory."""
    import torch

    if seed is not None:
        torch.manual_seed(seed)
        np.random.seed(seed)
    items = []
    for name, w in windows.items():
        x_ts = pd.Series(w.index)
        y_ts = pd.Series(future_sessions(w.index[-1], horizon))
        for _ in range(k):
            items.append((name, w.reset_index(drop=True), x_ts, y_ts))
    out: dict[str, list] = {n: [] for n in windows}
    for i in range(0, len(items), CHUNK):
        chunk = items[i: i + CHUNK]
        preds = pred.predict_batch([c[1] for c in chunk], [c[2] for c in chunk], [c[3] for c in chunk],
                                   pred_len=horizon, T=TEMP, top_p=TOP_P, sample_count=1, verbose=False)
        for (name, _, _, _), p in zip(chunk, preds):
            out[name].append(p[["open", "high", "low", "close", "volume", "amount"]].values)
    return {n: np.stack(v) for n, v in out.items() if v}


# ------------------------------------------------------------------ reading the paths


def stats(sym: str, w: pd.DataFrame, paths: np.ndarray, garch_sigd: float | None) -> dict:
    c0 = float(w["close"].iloc[-1])
    closes = paths[:, :, 3]
    r1 = closes[:, 0] / c0 - 1
    r5 = closes[:, -1] / c0 - 1
    kvol_d = float(np.std(np.log(np.clip(closes[:, 0], 1e-9, None) / c0), ddof=1))
    kvol5_d = float(np.std(np.log(np.clip(closes[:, -1], 1e-9, None) / c0), ddof=1) / math.sqrt(closes.shape[1]))
    ratio = kvol_d / garch_sigd if garch_sigd and np.isfinite(garch_sigd) and garch_sigd > 0 else None
    vol_flag = None if ratio is None else ("wider" if ratio > VOL_HI else "tighter" if ratio < VOL_LO else "agrees")
    p_up5 = float(np.mean(r5 > 0))
    view = "up" if p_up5 >= 0.65 else "down" if p_up5 <= 0.35 else "mixed"
    med_path = np.median(closes, axis=0)
    return dict(
        sym=sym, close=c0, paths=int(closes.shape[0]),
        ret1_med=float(np.median(r1)), ret5_med=float(np.median(r5)), ret5_mean=float(np.mean(r5)),
        ret5_p10=float(np.percentile(r5, 10)), ret5_p90=float(np.percentile(r5, 90)),
        p_up1=float(np.mean(r1 > 0)), p_up5=p_up5, view=view,
        kvol_d=kvol_d, kvol_ann=kvol_d * math.sqrt(engine.P["dpy"]), kvol5_d=kvol5_d,
        garch_sigd=garch_sigd, vol_ratio=ratio, vol_flag=vol_flag,
        median_path=[float(v) for v in med_path],
        p10_path=[float(v) for v in np.percentile(closes, 10, axis=0)],
        p90_path=[float(v) for v in np.percentile(closes, 90, axis=0)],
    )


def choose_names(latest: dict | None, mode: str) -> list[str]:
    if mode == "all" or not latest:
        return NAMES_ALL
    picks = [s["sym"] for s in latest["stocks"] if s.get("selected") or s.get("eligible")]
    core = [s for s in ("BBCA", "BMRI", "BBRI") if s in engine.UNIVERSE]
    return ["IHSG"] + sorted(set(picks) | set(core))


# ------------------------------------------------------------------ daily run


def run(raw: pd.DataFrame, latest: dict | None, outdir: Path, names: list[str] | None = None,
        k: int = PATHS, seed: int | None = None, held: list[str] | None = None) -> dict | None:
    t0 = time.time()
    pred = load_predictor()
    outdir.mkdir(parents=True, exist_ok=True)
    if pred is None:
        info = dict(version=VERSION, ok=False, reason="Kronos not available (torch/einops missing or no download)",
                    generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
        (outdir / "kronos.json").write_text(json.dumps(info))
        return info
    names = names or choose_names(latest, os.environ.get("KRONOS_NAMES", "watch"))
    held = set(held or [])
    windows, skipped = {}, []
    for s in names:
        try:
            w = bars(raw, s).tail(LOOKBACK)
        except Exception:
            skipped.append(s)
            continue
        if len(w) < LOOKBACK:
            skipped.append(s)
            continue
        windows[s] = w
    paths = sample_paths(pred, windows, k, HORIZON, seed)
    sig = {s["sym"]: s.get("sigd") for s in latest["stocks"]} if latest else {}
    if latest:
        sig["IHSG"] = latest["regime"]["ihsg_vol_ann"] / math.sqrt(engine.P["dpy"]) if latest["regime"].get("ihsg_vol_ann") else None
    status = {s["sym"]: s.get("status") for s in latest["stocks"]} if latest else {}
    rows = []
    for s, w in windows.items():
        if s not in paths:
            continue
        d = stats(s, w, paths[s], sig.get(s))
        d["bot_status"] = "Market index" if s == "IHSG" else status.get(s)
        d["held"] = s in held
        d["selected"] = bool(latest and any(x["sym"] == s and x.get("selected") for x in latest["stocks"]))
        rows.append(d)
    asof = max(w.index[-1] for w in windows.values()).strftime("%Y-%m-%d") if windows else None
    fut = [d.strftime("%Y-%m-%d") for d in future_sessions(pd.Timestamp(asof), HORIZON)] if asof else []
    hist_dates = list(windows["IHSG"].index[-60:]) if "IHSG" in windows else []
    out = dict(
        version=VERSION, ok=True, generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        asof=asof, model=MODEL, paths=k, lookback=LOOKBACK, horizon=HORIZON, temperature=TEMP, top_p=TOP_P,
        future_dates=fut, skipped=skipped, seconds=round(time.time() - t0, 1),
        ihsg_hist=dict(dates=[x.strftime("%Y-%m-%d") for x in hist_dates],
                       closes=[float(v) for v in windows["IHSG"]["close"].iloc[-60:]]) if hist_dates else None,
        names=sorted(rows, key=lambda r: (r["sym"] != "IHSG", not r["held"], not r["selected"], -r["p_up5"])),
        note="Advisory layer. Orders come from the engine's rules only. Kronos is logged forward for testing.",
    )
    (outdir / "kronos.json").write_text(json.dumps(engine.clean(out), separators=(",", ":")))
    log_daily(outdir / "kronos_log.csv", out)
    print(f"kronos: asof {asof} names {len(rows)} paths {k} in {out['seconds']}s; skipped {skipped}")
    return out


def log_daily(path: Path, out: dict) -> None:
    cols = ["date", "sym", "close", "ret1_med", "ret5_med", "p_up1", "p_up5", "ret5_p10", "ret5_p90",
            "kvol_d", "garch_sigd", "paths", "model"]
    old = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=cols)
    old = old[old["date"] != out["asof"]] if len(old) else old
    new = pd.DataFrame([{**{c: r.get(c) for c in cols if c not in ("date", "paths", "model")},
                         "date": out["asof"], "paths": out["paths"], "model": out["model"]} for r in out["names"]])
    pd.concat([old, new[cols]], ignore_index=True).to_csv(path, index=False, float_format="%.6g")


# ------------------------------------------------------------------ honest recent-days check


def evaluate(raw: pd.DataFrame, days: int, names: list[str], outdir: Path, k: int = PATHS, seed: int = 7) -> dict:
    """Walk forward over the last `days` sessions: forecast from data up to day t, compare with what happened."""
    pred = load_predictor()
    if pred is None:
        raise SystemExit("Kronos not available (install torch and einops).")
    full = {s: bars(raw, s) for s in names}
    full = {s: w for s, w in full.items() if len(w) >= LOOKBACK + days + HORIZON}
    recs = []
    for t in range(days, 0, -1):
        windows = {s: w.iloc[: len(w) - t - HORIZON].tail(LOOKBACK) for s, w in full.items()}
        # the window ends HORIZON+t sessions before the last bar, so 5 actual future closes exist for every forecast
        paths = sample_paths(pred, windows, k, HORIZON, seed + t)
        for s, w in windows.items():
            if s not in paths:
                continue
            d = stats(s, w, paths[s], None)
            end = len(full[s]) - t - HORIZON
            c0 = float(w["close"].iloc[-1])
            a1 = float(full[s]["close"].iloc[end]) / c0 - 1
            a5 = float(full[s]["close"].iloc[end + HORIZON - 1]) / c0 - 1
            hist = np.log(w["close"]).diff().dropna()
            ewma = float(np.sqrt(engine._ewma(hist).iloc[-1]))  # the engine's fallback vol, as a baseline
            recs.append(dict(date=w.index[-1].strftime("%Y-%m-%d"), sym=s, ret1_med=d["ret1_med"], ret5_med=d["ret5_med"],
                             p_up5=d["p_up5"], p10=d["ret5_p10"], p90=d["ret5_p90"], kvol=d["kvol_d"], ewma=ewma,
                             act1=a1, act5=a5))
        print(f"eval: {len(recs)} forecasts so far ({days - t + 1}/{days} days)", flush=True)
    df = pd.DataFrame(recs)
    out = dict(version=VERSION, generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), model=MODEL,
               paths=k, days=days, names=names, forecasts=len(df), window=[df["date"].min(), df["date"].max()])
    if len(df):
        out["by_name"] = {s: _score(g) for s, g in df.groupby("sym")}
        out["all"] = _score(df)
        df.to_csv(outdir / "kronos_eval.csv", index=False, float_format="%.6g")
    (outdir / "kronos_eval.json").write_text(json.dumps(engine.clean(out), separators=(",", ":")))
    return out


def _score(g: pd.DataFrame) -> dict:
    from scipy.stats import spearmanr

    def rank(a, b):
        if len(g) < 5 or np.std(a) == 0 or np.std(b) == 0:
            return None
        return float(spearmanr(a, b).correlation)

    hit1 = float(np.mean(np.sign(g["ret1_med"]) == np.sign(g["act1"])))
    hit5 = float(np.mean(np.sign(g["ret5_med"]) == np.sign(g["act5"])))
    up_base = float(np.mean(g["act5"] > 0))  # "always say up" baseline
    conf = g[(g["p_up5"] >= 0.65) | (g["p_up5"] <= 0.35)]
    hit5_conf = float(np.mean(np.sign(conf["ret5_med"]) == np.sign(conf["act5"]))) if len(conf) else None
    brier = float(np.mean((g["p_up5"] - (g["act5"] > 0)) ** 2))
    cover = float(np.mean((g["act5"] >= g["p10"]) & (g["act5"] <= g["p90"])))
    return dict(n=int(len(g)), hit_rate_1d=hit1, hit_rate_5d=hit5, always_up_5d=up_base,
                hit_rate_5d_confident=hit5_conf, n_confident=int(len(conf)), brier_5d=brier, brier_coin=0.25,
                band_coverage_5d=cover, band_target=0.80,
                corr_ret5=rank(g["ret5_med"], g["act5"]),
                vol_corr_kronos=rank(g["kvol"], g["act1"].abs()), vol_corr_ewma=rank(g["ewma"], g["act1"].abs()),
                mean_act5_when_up=float(g.loc[g["p_up5"] >= 0.65, "act5"].mean()) if (g["p_up5"] >= 0.65).any() else None,
                mean_act5_when_down=float(g.loc[g["p_up5"] <= 0.35, "act5"].mean()) if (g["p_up5"] <= 0.35).any() else None)


# ------------------------------------------------------------------ cli


def main() -> None:
    ap = argparse.ArgumentParser(description="Kronos advisory layer for the IDX Trend Bot")
    ap.add_argument("--cache", help="read raw prices from a pickle instead of downloading")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs" / "data"))
    ap.add_argument("--names", help="comma-separated names (default: IHSG + core banks + eligible), or 'all'")
    ap.add_argument("--paths", type=int, default=PATHS)
    ap.add_argument("--eval", type=int, metavar="DAYS", help="score forecasts over the last DAYS sessions instead")
    a = ap.parse_args()
    raw = pd.read_pickle(a.cache) if a.cache else engine.download()
    outdir = Path(a.out)
    names = None
    if a.names:
        names = NAMES_ALL if a.names == "all" else [n.strip().upper() for n in a.names.split(",")]
    if a.eval:
        res = evaluate(raw, a.eval, names or ["IHSG", "BBCA", "BMRI", "BBRI"], outdir, a.paths)
        print(json.dumps(res.get("all"), indent=1))
        return
    lp = outdir / "latest.json"
    latest = json.loads(lp.read_text()) if lp.exists() else None
    pp = outdir / "portfolio.json"
    held = list(json.loads(pp.read_text()).get("positions", {})) if pp.exists() else []
    run(raw, latest, outdir, names, a.paths, held=held)


if __name__ == "__main__":
    main()
