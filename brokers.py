"""Broker adapters. The rest of the bot only talks to this interface:
    positions() -> {sym: shares}, cash() -> float, submit(orders) -> None, fills(...) -> list

Levels of connectivity (see the framework spec, section 4d):
  PaperBroker  the bot's own model account (portfolio.py). Always runs; it is the benchmark.
  SlipBroker   a real IPOT / Stockbit account. No Indonesian broker offers a retail order API, so
               approved orders are written to docs/data/slip.json for Ben (or a browser assistant)
               to enter; Ben confirms fills with "/fill SYM PRICE LOTS" in Telegram and the real
               ledger (docs/data/real_account.json) tracks positions, cash and fill fidelity.
  CcxtBroker / IbkrBroker  placeholders for API venues (crypto via Indodax/Tokocrypto, IBKR).
               They raise until a strategy for those venues has been validated separately.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import portfolio


class PaperBroker:
    name = "paper"

    def __init__(self, path: Path, day: str, start_cash: float):
        self.path = path
        self.st = portfolio.load(path, day, start_cash)

    def positions(self) -> dict[str, int]:
        return {k: int(v["shares"]) for k, v in self.st["positions"].items() if v["shares"] > 0}

    def cash(self) -> float:
        return float(self.st["cash"])

    def submit(self, orders: list[dict]) -> None:
        self.st["pending"] = orders
        portfolio.save(self.path, self.st)

    def save(self) -> None:
        portfolio.save(self.path, self.st)


class SlipBroker:
    """Real account, human-entered. Keeps its own ledger so fills can be reconciled."""
    name = "slip"

    def __init__(self, slip_path: Path, ledger_path: Path, start_cash: float):
        self.slip_path, self.ledger_path = slip_path, ledger_path
        self.ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else dict(
            cash=float(start_cash), positions={}, fills=[], open_orders=[])

    def positions(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.ledger["positions"].items() if v > 0}

    def cash(self) -> float:
        return float(self.ledger["cash"])

    def submit(self, orders: list[dict]) -> None:
        self.ledger["open_orders"] = [dict(o, submitted_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
                                      for o in orders]
        slip = dict(session=orders[0]["session"] if orders else None, orders=orders,
                    note="Enter these in your broker app before the open. Confirm each fill in Telegram with: "
                         "/fill SYM PRICE LOTS. Unconfirmed orders expire at the end of the session.")
        self.slip_path.write_text(json.dumps(slip, separators=(",", ":")))
        self.save()

    def record_fills(self, fills: list[dict], fees: tuple[float, float]) -> list[dict]:
        done = []
        for f in fills:
            o = next((o for o in self.ledger["open_orders"] if o["sym"] == f["sym"]), None)
            side = o["side"] if o else ("sell" if f["sym"] in self.ledger["positions"] else "buy")
            shares = int(f["lots"]) * 100
            gross = shares * float(f["price"])
            if side == "buy":
                self.ledger["cash"] -= gross * (1 + fees[0])
                self.ledger["positions"][f["sym"]] = self.ledger["positions"].get(f["sym"], 0) + shares
            else:
                self.ledger["cash"] += gross * (1 - fees[1])
                self.ledger["positions"][f["sym"]] = max(0, self.ledger["positions"].get(f["sym"], 0) - shares)
                if self.ledger["positions"][f["sym"]] == 0:
                    del self.ledger["positions"][f["sym"]]
            rec = dict(sym=f["sym"], side=side, shares=shares, price=float(f["price"]),
                       limit=o["limit"] if o else None, proposal_id=o.get("proposal_id") if o else None,
                       at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
            self.ledger["fills"].append(rec)
            done.append(rec)
            if o:
                self.ledger["open_orders"].remove(o)
        self.ledger["fills"] = self.ledger["fills"][-500:]
        self.save()
        return done

    def expire_open(self) -> list[dict]:
        old, self.ledger["open_orders"] = self.ledger["open_orders"], []
        self.save()
        return old

    def save(self) -> None:
        self.ledger_path.write_text(json.dumps(self.ledger, separators=(",", ":")))


class CcxtBroker:
    name = "ccxt"

    def __init__(self, *a, **k):
        raise NotImplementedError("API venues (Indodax/Tokocrypto via ccxt) need their own validated strategy first.")


class IbkrBroker:
    name = "ibkr"

    def __init__(self, *a, **k):
        raise NotImplementedError("Interactive Brokers adapter: not enabled until a global-equity sleeve is validated.")
