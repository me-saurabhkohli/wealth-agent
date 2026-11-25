"""Compliance adapter: fixed rules applied to dates and quantities.

The article replaced a model with a rules engine, because a pass or fail
from rules can be reproduced from the inputs. This adapter is that idea:
the rules are declared as data (RULES below: id, name, description,
severity), and a small generic engine evaluates them. The engine mirrors
the naming in the article's compliance snippet: a RuleRowDecision is one
rule bound to its check, and a RuleSetDecision evaluates the whole set
and returns one result in the worker's contract shape.

Rules, in the order they are reported:

1. concentration_limit: after the proposed trades, no single ticker may
   hold more than 50 percent of the portfolio value.
2. wash_sale_window: buying a ticker within 30 days before or after a
   sale of the same ticker at a loss disallows the loss. The check runs
   against a precomputed conflict list built from recent sales, plus any
   loss sales inside the proposal itself, instead of asking a model to
   reason about dates.
3. drift_tolerance: after the trades, the allocation may sit at most 5
   points of drift from the target.
4. sell_within_holdings: no sale may exceed the shares actually held.
5. cash_residual: the trades must pay for themselves. Buys may not
   exceed sale proceeds (beyond a rounding allowance), and unspent
   proceeds may not exceed 5 percent of the portfolio.

Input JSON:
{
  "trades":       [ ... trade dicts from the tax adapter ... ],
  "holdings":     [ ... current holdings ... ],
  "prices":       {"AAPL": 175.20, ...},
  "targets":      {"stock": 0.6, "bond": 0.4},
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
MAX_DRIFT_POINTS = 5.0    # post-trade drift tolerance, percentage points
MAX_UNSPENT_PCT = 5.0     # uninvested proceeds tolerance, percent of portfolio
ROUNDING_ALLOWANCE = 0.50  # dollars of slack for share rounding

RULES = [
    {
        "id": "concentration_limit",
        "name": "Concentration limit",
        "description": "After the trades, no single ticker holds more than 50 percent of the portfolio value.",
        "severity": "error",
    },
    {
        "id": "wash_sale_window",
        "name": "Wash sale window",
        "description": "No planned buy of a ticker that was sold at a loss within 30 days, in recent sales or in this plan.",
        "severity": "error",
    },
    {
        "id": "drift_tolerance",
        "name": "Drift tolerance",
        "description": "After the trades, allocation drift from the target is at most 5 points.",
        "severity": "error",
    },
    {
        "id": "sell_within_holdings",
        "name": "Sell within holdings",
        "description": "No planned sale exceeds the shares held in that ticker.",
        "severity": "error",
    },
    {
        "id": "cash_residual",
        "name": "Cash residual",
        "description": "Buys are funded by sale proceeds, and unspent proceeds stay under 5 percent of the portfolio.",
        "severity": "error",
    },
]


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


class Evaluation:
    """Everything the rules need, computed once from the payload: share
    positions before and after the trades, values, proceeds, drift."""

    def __init__(self, payload: dict) -> None:
        self.trades = payload["trades"]
        self.holdings = payload["holdings"]
        self.prices = {ticker: float(price) for ticker, price in payload["prices"].items()}
        self.targets = payload.get("targets") or {"stock": 0.6, "bond": 0.4}
        self.recent_sales = payload.get("recent_sales", [])
        self.as_of = _parse_date(payload.get("as_of", "2026-11-24"))
        self.conflicts = build_conflict_list(self.recent_sales, self.trades)

        self.positions_before = {h["ticker"]: float(h["shares"]) for h in self.holdings}
        self.positions_after = dict(self.positions_before)
        for trade in self.trades:
            delta = float(trade["shares"]) if trade["side"] == "buy" else -float(trade["shares"])
            self.positions_after[trade["ticker"]] = self.positions_after.get(trade["ticker"], 0.0) + delta

        self.total_before = sum(
            shares * self.prices[ticker] for ticker, shares in self.positions_before.items()
        )
        self.sell_proceeds = sum(float(t["proceeds"]) for t in self.trades if t["side"] == "sell")
        self.buy_proceeds = sum(float(t["proceeds"]) for t in self.trades if t["side"] == "buy")
        self.net_cash = self.sell_proceeds - self.buy_proceeds
        self.total_after_positions = sum(
            shares * self.prices[ticker]
            for ticker, shares in self.positions_after.items()
            if shares > 0
        )
        # The economic total after the trades: what the positions are
        # worth plus any sale proceeds the plan did not spend.
        self.total_after = self.total_after_positions + max(self.net_cash, 0.0)

    def class_values_after(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for holding in self.holdings:
            ticker = holding["ticker"]
            shares = self.positions_after.get(ticker, 0.0)
            if shares > 0:
                values[holding["asset_class"]] = (
                    values.get(holding["asset_class"], 0.0) + shares * self.prices[ticker]
                )
        return values

    def drift_after_points(self) -> float:
        if self.total_after <= 0:
            return 0.0
        class_values = self.class_values_after()
        return (
            sum(
                abs(class_values.get(cls, 0.0) / self.total_after - target)
                for cls, target in self.targets.items()
            )
            * 100
        )


# -- rule checks: each returns (passed, detail, reasons) -----------------


def _check_concentration(ctx: Evaluation):
    if ctx.total_after_positions <= 0:
        return False, "Portfolio would have no value after the proposed trades", [
            "Portfolio would have no value after the proposed trades"
        ]
    worst_ticker, worst_pct = None, 0.0
    for ticker, shares in ctx.positions_after.items():
        if shares <= 0:
            continue
        pct = shares * ctx.prices[ticker] / ctx.total_after_positions * 100
        if pct > worst_pct:
            worst_ticker, worst_pct = ticker, pct
    if worst_pct > MAX_POSITION_PCT:
        detail = (
            f"{worst_ticker} would be {worst_pct:.1f} percent of the portfolio, "
            f"over the {MAX_POSITION_PCT:.0f} percent limit"
        )
        return False, detail, [detail]
    detail = (
        f"Largest position after trades is {worst_ticker} at {worst_pct:.1f} percent, "
        f"within the {MAX_POSITION_PCT:.0f} percent limit"
    )
    return True, detail, []


def _check_wash_sale(ctx: Evaluation):
    reasons = []
    for trade in ctx.trades:
        if trade["side"] != "buy":
            continue
        for conflict in ctx.conflicts:
            if conflict["ticker"] != trade["ticker"]:
                continue
            if conflict["loss_date"] is None:
                reasons.append(
                    f"Buy of {trade['ticker']} in the same run as a proposed loss sale of "
                    f"{trade['ticker']} triggers the wash sale window"
                )
            elif abs((ctx.as_of - conflict["loss_date"]).days) <= WASH_SALE_WINDOW_DAYS:
                reasons.append(
                    f"Buy of {trade['ticker']} on {ctx.as_of.isoformat()} falls within "
                    f"{WASH_SALE_WINDOW_DAYS} days of a loss sale on "
                    f"{conflict['loss_date'].isoformat()} ({conflict['source']})"
                )
    if reasons:
        return False, "; ".join(reasons), reasons
    return True, "No buy falls inside a wash sale window", []


def _check_drift(ctx: Evaluation):
    drift = ctx.drift_after_points()
    if drift > MAX_DRIFT_POINTS:
        detail = (
            f"Post-trade drift is {drift:.1f} points from the target allocation, "
            f"over the {MAX_DRIFT_POINTS:.0f} point tolerance"
        )
        return False, detail, [detail]
    detail = (
        f"Post-trade drift is {drift:.1f} points from the target allocation, "
        f"within the {MAX_DRIFT_POINTS:.0f} point tolerance"
    )
    return True, detail, []


def _check_sell_within_holdings(ctx: Evaluation):
    sold: dict[str, float] = {}
    for trade in ctx.trades:
        if trade["side"] == "sell":
            sold[trade["ticker"]] = sold.get(trade["ticker"], 0.0) + float(trade["shares"])
    reasons = []
    for ticker, shares in sorted(sold.items()):
        held = ctx.positions_before.get(ticker, 0.0)
        if shares > held + 1e-9:
            reasons.append(
                f"Planned sale of {shares:.2f} shares of {ticker} exceeds the {held:.2f} shares held"
            )
    if reasons:
        return False, "; ".join(reasons), reasons
    return True, "No planned sale exceeds the shares held", []


def _check_cash_residual(ctx: Evaluation):
    if ctx.net_cash < -ROUNDING_ALLOWANCE:
        detail = (
            f"Planned buys of ${ctx.buy_proceeds:,.2f} exceed sale proceeds of "
            f"${ctx.sell_proceeds:,.2f} by ${-ctx.net_cash:,.2f}"
        )
        return False, detail, [detail]
    if ctx.total_before > 0:
        unspent_pct = max(ctx.net_cash, 0.0) / ctx.total_before * 100
        if unspent_pct > MAX_UNSPENT_PCT:
            detail = (
                f"Trades leave ${ctx.net_cash:,.2f} of sale proceeds uninvested, "
                f"{unspent_pct:.1f} percent of the portfolio, over the "
                f"{MAX_UNSPENT_PCT:.0f} percent limit"
            )
            return False, detail, [detail]
        detail = (
            f"Unspent proceeds after trades are ${max(ctx.net_cash, 0.0):,.2f}, "
            f"{unspent_pct:.1f} percent of the portfolio"
        )
        return True, detail, []
    return True, "No trades to fund", []


_CHECKS = {
    "concentration_limit": _check_concentration,
    "wash_sale_window": _check_wash_sale,
    "drift_tolerance": _check_drift,
    "sell_within_holdings": _check_sell_within_holdings,
    "cash_residual": _check_cash_residual,
}


class RuleRowDecision:
    """One rule from RULES bound to its check function. Evaluates to a
    single check entry in the worker's output shape."""

    def __init__(self, definition: dict) -> None:
        self.definition = definition
        self.rule_id = definition["id"]
        self._check = _CHECKS[self.rule_id]

    def evaluate(self, ctx: Evaluation) -> tuple[dict, list[str]]:
        passed, detail, reasons = self._check(ctx)
        entry = {
            "rule": self.rule_id,
            "status": "pass" if passed else "fail",
            "detail": detail,
        }
        return entry, reasons


class RuleSetDecision:
    """The whole rule set, mirroring the article's naming. Evaluates
    every rule in declaration order and folds the results into the
    compliance contract: overall status, per-rule checks, reasons."""

    def __init__(self, rules: list[dict] | None = None) -> None:
        self.rows = [RuleRowDecision(definition) for definition in (rules or RULES)]

    def evaluate(self, payload: dict) -> dict:
        ctx = Evaluation(payload)
        checks = []
        reasons: list[str] = []
        for row in self.rows:
            entry, row_reasons = row.evaluate(ctx)
            checks.append(entry)
            reasons.extend(row_reasons)
        status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
        return {"status": status, "checks": checks, "reasons": reasons}


def run_compliance_check(proposed_trades_json: str) -> str:
    payload = json.loads(proposed_trades_json)
    result = RuleSetDecision().evaluate(payload)
    return json.dumps(result)
