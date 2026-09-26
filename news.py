"""News and sentiment for the watchlist.

Monitoring only: the trading rules do not use these scores. Each day's score per
stock is appended to docs/data/sentiment_log.csv, so it can later be tested on
data no model could have seen in advance.

Sources: Google News (Indonesian edition) per stock and for the IHSG, plus the
CNBC Indonesia market feed. Scoring: Claude Haiku when ANTHROPIC_API_KEY is set,
otherwise a rough Indonesian/English keyword count.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; idx-trend-bot/1.0)"}
MODEL = "claude-haiku-4-5-20251001"
NAMES = {
    "BBCA": "Bank Central Asia", "BBRI": "Bank Rakyat Indonesia", "BMRI": "Bank Mandiri",
    "BBNI": "Bank Negara Indonesia", "BRIS": "Bank Syariah Indonesia", "TLKM": "Telkom Indonesia",
    "ISAT": "Indosat", "EXCL": "XLSmart", "TOWR": "Sarana Menara", "ASII": "Astra International",
    "UNTR": "United Tractors", "UNVR": "Unilever Indonesia", "ICBP": "Indofood CBP",
    "INDF": "Indofood Sukses Makmur", "MYOR": "Mayora", "KLBF": "Kalbe Farma", "CPIN": "Charoen Pokphand",
    "JPFA": "Japfa", "AMRT": "Alfamart", "MAPI": "Mitra Adiperkasa", "ACES": "Aspirasi Hidup",
    "ADRO": "Alamtri", "PTBA": "Bukit Asam", "ITMG": "Indo Tambangraya", "MEDC": "Medco",
    "PGAS": "Perusahaan Gas Negara", "AKRA": "AKR Corporindo", "ANTM": "Aneka Tambang",
    "INCO": "Vale Indonesia", "SMGR": "Semen Indonesia",
}
MARKET_FEEDS = [
    ("CNBC Indonesia", "https://www.cnbcindonesia.com/market/rss"),
    ("Google News", "https://news.google.com/rss/search?q=IHSG+when:1d&hl=id&gl=ID&ceid=ID:id"),
]
POS = ["naik", "menguat", "melonjak", "melesat", "meroket", "terbang", "laba", "untung", "cuan", "dividen",
       "rekor", "tertinggi", "tumbuh", "positif", "borong", "net buy", "akumulasi", "buyback", "ekspansi",
       "kontrak baru", "surplus", "rebound", "rally", "rises", "jumps", "surges", "record", "profit", "beats",
       "upgrade", "gains"]
NEG = ["turun", "melemah", "anjlok", "ambruk", "longsor", "rontok", "merosot", "rugi", "gagal bayar", "pkpu",
       "pailit", "suspensi", "gugatan", "denda", "korupsi", "tersangka", "negatif", "net sell", "downgrade",
       "terendah", "tertekan", "tekanan", "arb", "phk", "defisit", "koreksi", "falls", "drops", "plunges",
       "loss", "slump", "probe"]
POS_RE = re.compile(r"\b(" + "|".join(map(re.escape, POS)) + r")\b", re.I)
NEG_RE = re.compile(r"\b(" + "|".join(map(re.escape, NEG)) + r")\b", re.I)
TICK_RE = re.compile(r"\b(" + "|".join(NAMES) + r")\b")
ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)


def _tag(x: str, t: str) -> str:
    m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", x, re.S)
    return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""


def _get(url: str) -> str:
    for _ in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code == 200:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return ""


def _items(xml: str, default_source: str) -> list[dict]:
    out = []
    for x in ITEM_RE.findall(xml):
        title = re.sub(r"<[^>]+>", "", _tag(x, "title"))
        link, src = _tag(x, "link"), _tag(x, "source") or default_source
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        try:
            pub = parsedate_to_datetime(_tag(x, "pubDate")).astimezone(timezone.utc)
        except Exception:
            pub = datetime.now(timezone.utc)
        if title and link.startswith("http"):
            out.append(dict(title=title, link=link, source=src, published=pub.isoformat(timespec="minutes")))
    return out


def tickers_in(title: str) -> set[str]:
    found = set(TICK_RE.findall(title))
    low = title.lower()
    found |= {t for t, n in NAMES.items() if n.lower() in low}
    return found


def fetch(tickers: list[str], days: int = 2, per_ticker: int = 10) -> list[dict]:
    got: dict[str, dict] = {}

    def add(it: dict, tags: set[str]) -> None:
        key = hashlib.sha1(re.sub(r"\W+", "", it["title"].lower()).encode()).hexdigest()[:16]
        if key in got:
            got[key]["tickers"] = sorted(set(got[key]["tickers"]) | tags)
            return
        it.update(id=key, tickers=sorted(tags))
        got[key] = it

    for t in tickers:
        url = f"https://news.google.com/rss/search?q={quote('saham ' + t)}+when:{days}d&hl=id&gl=ID&ceid=ID:id"
        for it in _items(_get(url), "Google News")[:per_ticker]:
            add(it, tickers_in(it["title"]))  # only tag stocks the headline names
        time.sleep(0.4)
    for src, url in MARKET_FEEDS:
        for it in _items(_get(url), src)[:40]:
            add(it, tickers_in(it["title"]))
    return list(got.values())


def keyword_score(title: str) -> float:
    p, n = len(POS_RE.findall(title)), len(NEG_RE.findall(title))
    return round((p - n) / (p + n), 2) if p + n else 0.0


def score_claude(items: list[dict], key: str) -> None:
    for i in range(0, len(items), 30):
        batch = items[i: i + 30]
        rows_in = [{"i": j, "title": it["title"], "tickers": it["tickers"]} for j, it in enumerate(batch)]
        prompt = (
            "Rate each Indonesian stock-market headline for its likely short-term effect on the listed IDX "
            "stocks (or on the IHSG if none are listed). Reply with only a JSON array, one object per headline: "
            '{"i": index, "s": number from -1 (very negative) to 1 (very positive), 0 if neutral or unclear, '
            '"t": [IDX tickers clearly affected], "e": one of earnings, dividend, corporate_action, legal, macro, '
            "flows, analyst, management, operations, other}.\n\n" + json.dumps(rows_in, ensure_ascii=False))
        rows: dict[int, dict] = {}
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=90, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            rows = {int(x["i"]): x for x in json.loads(text)}
        except Exception as exc:  # fall back to keywords for this batch
            print("Sentiment: Claude scoring failed, using keywords:", type(exc).__name__)
        for j, it in enumerate(batch):
            x = rows.get(j)
            if x is None:
                it["s"], it["m"] = keyword_score(it["title"]), "keywords"
                continue
            it["s"] = round(max(-1.0, min(1.0, float(x.get("s", 0) or 0))), 2)
            extra = {t for t in x.get("t", []) if t in NAMES}
            it["tickers"] = sorted(set(it["tickers"]) | extra)
            it["e"], it["m"] = str(x.get("e", "other"))[:20], "claude"


def update(path: Path, tickers: list[str], key: str | None = None, keep_days: int = 5, days: int = 2):
    """Fetch, score only headlines not seen before, merge, trim. Returns (all_items, new_items)."""
    old = json.loads(path.read_text()) if path.exists() else {"items": []}
    known = {it["id"]: it for it in old.get("items", [])}
    fresh = [it for it in fetch(tickers, days=days) if it["id"] not in known]
    if key:
        score_claude(fresh, key)
    else:
        for it in fresh:
            it["s"], it["m"] = keyword_score(it["title"]), "keywords"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="minutes")
    fresh = [it for it in fresh if it["published"] >= cutoff]
    items = sorted([it for it in known.values() if it["published"] >= cutoff] + fresh,
                   key=lambda it: it["published"], reverse=True)[:800]
    path.write_text(json.dumps(dict(updated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                                    method="claude" if key else "keywords", items=items),
                               ensure_ascii=False, separators=(",", ":")))
    return items, fresh


def summary(items: list[dict], hours: int = 24) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="minutes")
    out: dict[str, dict] = {}
    for it in items:
        if it["published"] < cutoff:
            continue
        for t in it["tickers"] or ["MARKET"]:
            d = out.setdefault(t, dict(n=0, total=0.0, worst=None, best=None))
            s = it.get("s", 0.0)
            d["n"] += 1
            d["total"] += s
            brief = dict(title=it["title"], s=s, link=it["link"])
            if d["worst"] is None or s < d["worst"]["s"]:
                d["worst"] = brief
            if d["best"] is None or s > d["best"]["s"]:
                d["best"] = brief
    for d in out.values():
        d["avg"] = round(d.pop("total") / d["n"], 3)
    return out


def log_daily(path: Path, day: str, summ: dict) -> None:
    if path.exists() and f"\n{day}," in "\n" + path.read_text():
        return  # already logged today
    lines = [] if path.exists() else ["date,ticker,headlines,avg_sentiment"]
    lines += [f"{day},{t},{d['n']},{d['avg']}" for t, d in sorted(summ.items())]
    with path.open("a") as fh:
        fh.write("\n".join(lines) + "\n")
