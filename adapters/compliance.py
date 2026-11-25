"""Compliance adapter: fixed rules applied to dates and quantities.

The article replaced a model with a rules engine, because a pass or fail
from rules can be reproduced from the inputs. This adapter is that idea in
plain Python functions, with two rules:

1. Concentration: after the proposed trades, no single ticker may hold
   more than MAX_POSITION_PCT of the portfolio value.
2. Wash sale: buying a ticker within 30 days before or after a sale of the
   same ticker at a loss disallows the loss. The check runs against a
   precomputed conflict list built from recent sales, plus any loss sales
   inside the proposal itself, instead of asking a model to reason about
   dates.

Input JSON:
{
  "trades":       [ ... trade dicts from the tax adapter ... ],
  "holdings":     [ ... current holdings ... ],
  "prices":       {"AAPL": 175.20, ...},
  "recent_sales": [{"ticker": "AAPL", "date": "2026-11-10",
                    "realized_gain_loss": -500.0}],
  "as_of": "2026-11-24"
}

Output JSON: {"status": "pass" | "fail", "checks": [...], "reasons": [...]}
"""

from __future__ import annotations

import json
from datetime import date

MAX_POSITION_PCT = 50.0   # demo mandate limit, percent of portfolio value
WASH_SALE_WINDOW_DAYS = 30


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def build_conflict_list(recent_sales: list[dict], trades: list[dict]) -> list[dict]:
    """Precompute every (ticker, loss date) pair a buy could conflict with:
    historical loss sales, and loss sales proposed in this same run."""
    conflicts = []
    for sale in recent_sales:
        if float(sale.get("realized_gain_loss", 0)) < 0:
            conflicts.append({"ticker": sale["ticker"], "loss_date": _parse_date(sale["date"]), "source": "recent sale"})
    for trade in trades:
        if trade["side"] == "sell" and float(trade.get("realized_gain_loss", 0)) < 0:
            conflicts.append({"ticker": trade["ticker"], "loss_date": None, "source": "proposed sale in this run"})
    return conflicts


def check_concentration(trades: list[dict], holdings: list[dict], prices: dict) -> tuple[bool, str]:
    positions = {h["ticker"]: float(h["shares"]) for h in holdings}
    for trade in trades:
        delta = float(trade["shares"]) if trade["side"] == "buy" else -float(trade["shares"])
        positions[trade["ticker"]] = positions.get(trade["ticker"], 0.0) + delta
    total = sum(shares * prices[ticker] for ticker, shares in positions.items() if shares > 0)
    if total <= 0:
        return False, "Portfolio would have no value after the proposed trades"
    worst_ticker, worst_pct = None, 0.0
    for ticker, shares in positions.items():
        if shares <= 0:
            continue
        pct = shares * prices[ticker] / total * 100
        if pct > worst_pct:
            worst_ticker, worst_pct = ticker, pct
    if worst_pct > MAX_POSITION_PCT:
        return False, (
            f"{worst_ticker} would be {worst_pct:.1f} percent of the portfolio, "
            f"over the {MAX_POSITION_PCT:.0f} percent limit"
        )
    return True, f"Largest position after trades is {worst_ticker} at {worst_pct:.1f} percent, within the {MAX_POSITION_PCT:.0f} percent limit"


def check_wash_sale(trades: list[dict], conflicts: list[dict], as_of: date) -> tuple[bool, list[str]]:
    reasons = []
    for trade in trades:
        if trade["side"] != "buy":
            continue
        for conflict in conflicts:
            if conflict["ticker"] != trade["ticker"]:
                continue
            if conflict["loss_date"] is None:
                reasons.append(
                    f"Buy of {trade['ticker']} in the same run as a proposed loss sale of "
                    f"{trade['ticker']} triggers the wash sale window"
                )
            elif abs((as_of - conflict["loss_date"]).days) <= WASH_SALE_WINDOW_DAYS:
                reasons.append(
                    f"Buy of {trade['ticker']} on {as_of.isoformat()} falls within "
                    f"{WASH_SALE_WINDOW_DAYS} days of a loss sale on "
                    f"{conflict['loss_date'].isoformat()} ({conflict['source']})"
                )
    return (len(reasons) == 0), reasons


def run_compliance_check(proposed_trades_json: str) -> str:
    payload = json.loads(proposed_trades_json)
    trades = payload["trades"]
    holdings = payload["holdings"]
    prices = {ticker: float(price) for ticker, price in payload["prices"].items()}
    recent_sales = payload.get("recent_sales", [])
    as_of = _parse_date(payload.get("as_of", date.today().isoformat()))

    conflicts = build_conflict_list(recent_sales, trades)

    checks = []
    reasons = []

    ok, detail = check_concentration(trades, holdings, prices)
    checks.append({"rule": "concentration", "status": "pass" if ok else "fail", "detail": detail})
    if not ok:
        reasons.append(detail)

    ok, wash_reasons = check_wash_sale(trades, conflicts, as_of)
    checks.append(
        {
            "rule": "wash_sale",
            "status": "pass" if ok else "fail",
            "detail": "No buy falls inside a wash sale window" if ok else "; ".join(wash_reasons),
        }
    )
    reasons.extend(wash_reasons)

    status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
    return json.dumps({"status": status, "checks": checks, "reasons": reasons})
