"""Tax adapter: the worker-facing wrapper around the Oracle optimizer.

Interface matches the article: JSON string in, JSON string out, with
recommended_trades, tax_savings_estimate, and drift_reduction.

One honest difference from the article's trimmed snippet: the snippet took
holdings alone and hard-coded a 60/40 target inside the function. Here the
input JSON carries holdings, prices, and targets together, so the function
stays pure. Same inputs, same outputs, every run.

Input shape:
{
  "holdings": [
    {"ticker": "AAPL", "asset_class": "stock", "shares": 100,
     "lots": [{"shares": 30, "cost_basis": 120.0, "acquired": "2023-03-01"},
              {"shares": 70, "cost_basis": 190.0, "acquired": "2025-09-15"}]}
  ],
  "prices": {"AAPL": 175.20},
  "targets": {"stock": 0.6, "bond": 0.4}
}
"""

from __future__ import annotations

import json

from .oracle import Oracle, OracleStrategy


def run_tax_optimization(holdings_json: str) -> str:
    payload = json.loads(holdings_json)
    holdings = payload["holdings"]
    prices = {ticker: float(price) for ticker, price in payload["prices"].items()}
    targets = payload.get("targets") or {"stock": 0.6, "bond": 0.4}

    strategy = OracleStrategy(targets=targets, optimization_type="TAX_AWARE")
    oracle = Oracle(strategy=strategy, holdings=holdings, prices=prices)
    result = oracle.optimize()

    return json.dumps(
        {
            "recommended_trades": [trade.to_dict() for trade in result.trades],
            "tax_savings_estimate": result.tax_savings_estimate,
            "drift_reduction": result.drift_reduction,
            "net_realized_gain_loss": result.net_realized_gain_loss,
            "drift_before_pct": result.drift_before,
            "drift_after_pct": result.drift_after,
        }
    )
