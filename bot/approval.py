"""Approval layer. Every order is a *proposal* until someone (Ben, or a policy) approves it.

Flow (weekdays, WIB):
  17:30  EOD job builds proposals -> checkers -> committee -> stored in docs/data/proposals.json
         and sent to Telegram (with Approve / Halve / Reject buttons when the mode needs a tap)
  08:30  pre-open job pulls the taps (Telegram getUpdates), applies the mode's default to the
         rest, writes the final order list for the broker and appends the audit trail
         (docs/data/decisions.jsonl).

Modes (env APPROVAL_MODE):
  manual - every non-blocked proposal waits for a tap; no tap by 08:30 = rejected
  policy - proposals that pass all checkers and are not vetoed by the committee are approved at
           once; committee vetoes are escalated to Ben (a tap can override the LLM, never a
           deterministic block); no tap = rejected
  auto   - like policy, but vetoes are applied without asking (paper accounts / API venues)
Sells (risk-reducing) are always approved in policy/auto unless the risk gate is HALTED.

Telegram commands understood by the collector (only from TELEGRAM_CHAT_ID):
  /halt, /resume, /status, /fill SYM PRICE LOTS (records a real fill for the slip broker)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

MODES = ("manual", "policy", "auto")


def mode() -> str:
    m = (os.environ.get("APPROVAL_MODE") or "auto").lower().strip()
    return m if m in MODES else "auto"


def _api(method: str, **payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    if r.status_code != 200:
        print("Telegram", method, r.status_code, r.text[:200])
        return None
    return r.json().get("result")


def pid(session: str, sym: str, side: str) -> str:
    return f"{session}-{sym}-{side}"


def build_proposals(orders: list[dict], latest: dict, rules: list[dict], risk: list[dict],
                    committee: list[dict], cash_check: dict, data_check: dict) -> list[dict]:
    """Merge the order list with every checker's verdict into proposal records."""
    stocks = {s["sym"]: s for s in latest["stocks"]}
    session = latest["next_session"]
    out = []
    for o, ru, ri, co in zip(orders, rules, risk, committee):
        s = stocks[o["sym"]]
        blocked = []
        if not ru["ok"]:
            blocked += ru["violations"]
        if ri["verdict"] == "block":
            blocked.append(ri["limit"])
        if not cash_check["ok"] and o["side"] == "buy":
            blocked.append(cash_check["note"])
        shares = o["shares"]
        if ri["verdict"] == "downsize":
            shares = ri["shares"]
        size_mult = co.get("size", 1.0) if co.get("valid") else 1.0
        p = dict(
            id=pid(session, o["sym"], o["side"]), session=session, asof=latest["asof"],
            created_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sym=o["sym"], side=o["side"], lots=o["lots"], shares=o["shares"], limit=o["limit"],
            ref_close=s["close"], ara=o["ara"], arb=o["arb"], why=o["why"],
            rank=s.get("rank"), target_w=s.get("target_w"), sigd=s.get("sigd"),
            checks=dict(data=data_check["ok"], rules=ru, risk=ri, cash=cash_check["ok"]),
            committee=co, blocked=blocked,
            final_shares=int(shares * size_mult) // 100 * 100 if not blocked else 0,
            status="blocked" if blocked else "pending", approval=None,
        )
        out.append(p)
    return out


def decide_immediately(props: list[dict], m: str, gate_state: str) -> None:
    """Apply the mode before anything is sent: what needs a tap, what is decided now."""
    for p in props:
        if p["status"] == "blocked":
            continue
        co = p["committee"]
        if p["side"] == "sell" and gate_state != "HALTED" and m != "manual":
            p["status"], p["approval"] = "approved", dict(by="policy", note="risk-reducing exit")
        elif m == "manual":
            p["status"] = "awaiting"
        elif co.get("verdict") == "veto":
            if m == "auto":
                p["status"], p["approval"] = "vetoed", dict(by="committee", note=co.get("reason", ""))
                p["final_shares"] = 0
            else:
                p["status"] = "awaiting"  # policy: Ben can override the LLM
        else:
            note = "within limits" + (", halved by committee caution" if co.get("verdict") == "caution" else "")
            p["status"], p["approval"] = "approved", dict(by="policy", note=note)
        if p["status"] == "approved" and p["final_shares"] <= 0:
            p["status"], p["approval"] = "rejected", dict(by="policy", note="size rounds to zero lots")


def render(p: dict, rp) -> str:
    c = p["committee"]
    verdict = {"agree": "committee agrees", "caution": "committee: caution (half size)",
               "veto": "committee: VETO", "unavailable": "committee not run",
               "invalid": "committee output invalid (ignored)"}.get(c.get("verdict"), "")
    head = f"{'BUY' if p['side'] == 'buy' else 'SELL'} {p['sym']} {p['final_shares'] // 100 or p['lots']} lots, limit {rp(p['limit'])}"
    lines = [head, p["why"]]
    if p["blocked"]:
        lines.append("BLOCKED: " + "; ".join(p["blocked"]))
    else:
        r = p["checks"]["risk"]
        lines.append("Checks: rules OK, risk " + (r["verdict"] + (" (" + r["limit"] + ")" if r["limit"] else "")))
        if c.get("verdict") in ("unavailable", "invalid"):
            lines.append("Committee: " + ("not run (no ANTHROPIC_API_KEY)" if c.get("verdict") == "unavailable" else "output invalid, ignored"))
        elif verdict:
            lines.append(verdict + (": " + c["reason"] if c.get("reason") else ""))
    lines.append("Status: " + p["status"] + (f" ({p['approval']['note']})" if p.get("approval") and p["approval"].get("note") else ""))
    return "\n".join(lines)


def send_proposals(props: list[dict], rp, esc, dry: bool) -> None:
    """One Telegram message per proposal; buttons only when the proposal is awaiting a tap."""
    for p in props:
        text = esc(render(p, rp))
        kb = None
        if p["status"] == "awaiting":
            kb = {"inline_keyboard": [[
                {"text": "Approve", "callback_data": "a|" + p["id"]},
                {"text": "Halve", "callback_data": "h|" + p["id"]},
                {"text": "Reject", "callback_data": "r|" + p["id"]}]]}
        if dry or not os.environ.get("TELEGRAM_TOKEN"):
            print("---- proposal (not sent) ----\n" + render(p, rp) + ("\n[buttons]" if kb else ""))
            continue
        res = _api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text, parse_mode="HTML",
                   reply_markup=kb) if kb else _api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"],
                                                       text=text, parse_mode="HTML")
        if res:
            p["message_id"] = res.get("message_id")


def collect(store: dict, offset_path: Path, gate, fills: list[dict]) -> list[str]:
    """Pull Telegram updates: button taps on proposals and text commands. Returns human-readable events."""
    events: list[str] = []
    chat = str(os.environ.get("TELEGRAM_CHAT_ID") or "")
    if not os.environ.get("TELEGRAM_TOKEN"):
        return events
    off = json.loads(offset_path.read_text()).get("offset", 0) if offset_path.exists() else 0
    updates = _api("getUpdates", offset=off, timeout=0, allowed_updates=["callback_query", "message"]) or []
    by_id = {p["id"]: p for p in store.get("items", [])}
    for u in updates:
        off = max(off, u["update_id"] + 1)
        cq, msg = u.get("callback_query"), u.get("message")
        if cq:
            if str(cq.get("from", {}).get("id")) != chat and str(cq.get("message", {}).get("chat", {}).get("id")) != chat:
                continue
            act, _, ident = (cq.get("data") or "").partition("|")
            p = by_id.get(ident)
            if p and p["status"] == "awaiting":
                when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                if act == "a":
                    p["status"], p["approval"] = "approved", dict(by="ben", at=when, note="approved by tap")
                elif act == "h":
                    p["final_shares"] = (p["final_shares"] // 2) // 100 * 100
                    p["status"] = "approved" if p["final_shares"] > 0 else "rejected"
                    p["approval"] = dict(by="ben", at=when, note="halved by tap")
                elif act == "r":
                    p["status"], p["approval"] = "rejected", dict(by="ben", at=when, note="rejected by tap")
                events.append(f"{p['id']}: {p['status']}")
                _api("answerCallbackQuery", callback_query_id=cq["id"], text=f"{p['sym']} {p['status']}")
                if cq.get("message"):
                    _api("editMessageReplyMarkup", chat_id=cq["message"]["chat"]["id"],
                         message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
            else:
                _api("answerCallbackQuery", callback_query_id=cq["id"], text="already decided")
        elif msg and str(msg.get("chat", {}).get("id")) == chat:
            t = (msg.get("text") or "").strip()
            if t.startswith("/halt"):
                gate.set("HALTED", "manual /halt from Telegram", manual=True)
                events.append("risk gate HALTED by /halt")
            elif t.startswith("/resume"):
                gate.set("ACTIVE", "manual /resume from Telegram", manual=True)
                gate.state["manual"] = False
                gate.save()
                events.append("risk gate ACTIVE by /resume")
            elif t.startswith("/status"):
                n = sum(1 for p in by_id.values() if p["status"] == "awaiting")
                _api("sendMessage", chat_id=chat, text=f"Risk gate {gate.state['state']} ({gate.state.get('reason') or 'ok'}). "
                                                       f"{n} proposal(s) awaiting your tap. Mode: {mode()}.")
            elif t.startswith("/fill"):
                parts = t.split()
                if len(parts) == 4:
                    try:
                        fills.append(dict(sym=parts[1].upper(), price=float(parts[2].replace(",", "")),
                                          lots=int(parts[3]), at=msg.get("date")))
                        events.append(f"fill recorded: {parts[1].upper()} {parts[3]} lots at {parts[2]}")
                    except ValueError:
                        pass
    offset_path.write_text(json.dumps({"offset": off}))
    return events


def expire(store: dict, m: str) -> None:
    for p in store.get("items", []):
        if p["status"] == "awaiting":
            p["status"] = "rejected"
            p["approval"] = dict(by="policy", note="no tap before the open (" + m + " mode default)")


def approved_orders(store: dict) -> list[dict]:
    out = []
    for p in store.get("items", []):
        if p["status"] == "approved" and p["final_shares"] > 0:
            out.append(dict(side=p["side"], sym=p["sym"], shares=p["final_shares"], lots=p["final_shares"] // 100,
                            limit=p["limit"], ara=p["ara"], arb=p["arb"], why=p["why"], session=p["session"],
                            ref_close=p["ref_close"], proposal_id=p["id"]))
    return out


def log_decisions(path: Path, store: dict, extra: dict | None = None) -> int:
    """Append every decided proposal once (idempotent by id + status) to decisions.jsonl."""
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                d = json.loads(line)
                seen.add((d["id"], d["status"]))
            except Exception:
                continue
    n = 0
    with path.open("a") as f:
        for p in store.get("items", []):
            if p["status"] in ("pending", "awaiting") or (p["id"], p["status"]) in seen:
                continue
            rec = dict(p)
            rec["logged_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            rec["mode"] = store.get("mode")
            if extra:
                rec.update(extra)
            rec["hash"] = hashlib.sha1(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()[:10]
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            n += 1
    return n


# ------------------------------------------------------------------ approvals from the dashboard
# The dashboard's Approve / Halve / Reject buttons open a pre-filled GitHub issue
# ("approve:<proposal id>"). The approvals workflow records it in docs/data/approvals.json;
# the pre-open / collect jobs apply it here. Only issues opened by the repository owner count.
ACTIONS = {"approve": "a", "halve": "h", "reject": "r"}


def record_external(path: Path, title: str, by: str, owner: str) -> dict | None:
    m = re.match(r"^(approve|halve|reject):([A-Za-z0-9._-]+)$", (title or "").strip().lower())
    if not m:
        return None
    if by.lower() != owner.lower():
        return dict(ignored=True, reason="not the repository owner")
    data = json.loads(path.read_text()) if path.exists() else dict(decisions=[])
    d = dict(id=m.group(2).upper() if m.group(2)[:10].count("-") < 3 else m.group(2), action=m.group(1), by=by,
             at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), applied=False)
    # proposal ids are like 2026-09-28-AKRA-buy: keep the date lower/upper mix as stored
    d["id"] = _canon(m.group(2))
    data["decisions"].append(d)
    path.write_text(json.dumps(data, separators=(",", ":")))
    return d


def _canon(ident: str) -> str:
    parts = ident.split("-")
    if len(parts) >= 5:
        return "-".join(parts[:3]) + "-" + parts[3].upper() + "-" + "-".join(parts[4:]).lower()
    return ident


def apply_external(stores: list[dict], path: Path) -> list[str]:
    """Apply recorded dashboard decisions to awaiting proposals (idempotent)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    by_id = {p["id"]: p for st in stores for p in st.get("items", [])}
    events = []
    changed = False
    for d in data.get("decisions", []):
        if d.get("applied"):
            continue
        p = by_id.get(d["id"])
        if not p:
            continue  # proposal not in the current stores yet (or long gone); leave it for later
        if p["status"] == "awaiting":
            act = ACTIONS[d["action"]]
            if act == "a":
                p["status"], p["approval"] = "approved", dict(by=d["by"], at=d["at"], note="approved on the dashboard")
            elif act == "h":
                p["final_shares"] = (p["final_shares"] // 2) // 100 * 100
                p["status"] = "approved" if p["final_shares"] > 0 else "rejected"
                p["approval"] = dict(by=d["by"], at=d["at"], note="halved on the dashboard")
            else:
                p["status"], p["approval"] = "rejected", dict(by=d["by"], at=d["at"], note="rejected on the dashboard")
            events.append(f"{p['id']}: {p['status']} (dashboard)")
        d["applied"] = True
        changed = True
    if changed:
        data["decisions"] = data["decisions"][-300:]
        path.write_text(json.dumps(data, separators=(",", ":")))
    return events
