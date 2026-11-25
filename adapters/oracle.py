"""Oracle: deterministic, tax-aware portfolio optimization.

The article calls the tax library "Oracle" and imports it from a private
service package. That package is not public, so this module is a
self-contained implementation with the same job and the same output shape:
given holdings with tax lots, current prices, and a target allocation, it
produces tax-lot aware trade recommendations, a tax savings estimate, and
the drift reduction. Pure Python, no model calls, no randomness, no clock.
The same inputs always produce the same trades.

Method, kept deliberately simple and auditable:

1. Value the portfolio by asset class and measure drift from target.
2. If one class is overweight, sell from that class. Lots are sold in a
   fixed order: losses first (largest loss per share first, so losses are
   harvested), then the smallest gain per share. Specific lot
   identification, partial lots allowed.
3. Spend the proceeds on the underweight class, buying its existing
   tickers in proportion to their current value.
4. Tax savings estimate: harvested net losses valued at an assumed
   marginal rate (TAX_RATE below). If the run realizes a net gain instead,
   the estimate is zero and the realized gain is reported on the trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Assumed combined marginal rate used only to turn a harvested loss into a
# savings estimate. It is an assumption of the demo, not tax advice.
TAX_RATE = 0.25


@dataclass
class Trade:
    ticker: str
    side: str            # "sell" or "buy"
    shares: float
    price: float
    proceeds: float      # shares * price, positive
    realized_gain_loss: float = 0.0   # sells only; buys are 0.0
    lot_cost_basis: float | None = None  # per share, sells only

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "shares": round(self.shares, 2),
            "price": round(self.price, 2),
            "proceeds": round(self.proceeds, 2),
            "realized_gain_loss": round(self.realized_gain_loss, 2),
            "lot_cost_basis": None if self.lot_cost_basis is None else round(self.lot_cost_basis, 2),
        }


@dataclass
class OracleResult:
    trades: list[Trade] = field(default_factory=list)
    tax_savings_estimate: float = 0.0
    drift_reduction: float = 0.0
    net_realized_gain_loss: float = 0.0
    drift_before: float = 0.0
    drift_after: float = 0.0


class OracleStrategy:
    """Strategy settings, mirroring the article's OracleStrategy shape."""

    def __init__(self, targets: dict[str, float], optimization_type: str = "TAX_AWARE") -> None:
        total = sum(targets.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Target allocation must sum to 1.0, got {total}")
        self.targets = dict(targets)
        self.optimization_type = optimization_type


class Oracle:
    """The optimizer. Construct with a strategy, then call optimize()."""

    def __init__(self, strategy: OracleStrategy, holdings: list[dict], prices: dict[str, float]) -> None:
        self.strategy = strategy
        self.holdings = holdings
        self.prices = prices

    def _class_values(self, positions: dict[str, float]) -> dict[str, float]:
        values: dict[str, float] = {}
        for holding in self.holdings:
            ticker = holding["ticker"]
            values[holding["asset_class"]] = (
                values.get(holding["asset_class"], 0.0)
                + positions.get(ticker, 0.0) * self.prices[ticker]
            )
        return values

    @staticmethod
    def _drift(class_values: dict[str, float], total: float, targets: dict[str, float]) -> float:
        return sum(
            abs(class_values.get(cls, 0.0) / total - target)
            for cls, target in targets.items()
        )

    def optimize(self) -> OracleResult:
        targets = self.strategy.targets
        positions = {h["ticker"]: float(h["shares"]) for h in self.holdings}
        total = sum(positions[t] * self.prices[t] for t in positions)
        if total <= 0:
            raise ValueError("Portfolio has no value to rebalance")

        class_values = self._class_values(positions)
        drift_before = self._drift(class_values, total, targets)

        trades: list[Trade] = []
        net_realized = 0.0

        overweight = {
            cls: class_values.get(cls, 0.0) - targets[cls] * total
            for cls in targets
            if class_values.get(cls, 0.0) - targets[cls] * total > 0.01
        }
        underweight = {
            cls: targets[cls] * total - class_values.get(cls, 0.0)
            for cls in targets
            if targets[cls] * total - class_values.get(cls, 0.0) > 0.01
        }

        for cls, excess in sorted(overweight.items()):
            remaining = excess
            # Candidate lots in this class: losses first (deepest loss per
            # share first), then smallest gain per share. Ties break on
            # ticker and basis so the order is fully deterministic.
            lots = []
            for holding in self.holdings:
                if holding["asset_class"] != cls:
                    continue
                price = self.prices[holding["ticker"]]
                for lot in holding.get("lots", []):
                    gain_per_share = price - float(lot["cost_basis"])
                    lots.append((gain_per_share, holding["ticker"], float(lot["cost_basis"]), float(lot["shares"])))
            lots.sort(key=lambda item: (item[0], item[1], item[2]))

            for gain_per_share, ticker, basis, lot_shares in lots:
                if remaining <= 0.01:
                    break
                price = self.prices[ticker]
                available_value = min(lot_shares, positions[ticker]) * price
                sell_value = min(remaining, available_value)
                shares = round(sell_value / price, 4)
                if shares <= 0:
                    continue
                realized = shares * gain_per_share
                trades.append(
                    Trade(
                        ticker=ticker,
                        side="sell",
                        shares=shares,
                        price=price,
                        proceeds=shares * price,
                        realized_gain_loss=realized,
                        lot_cost_basis=basis,
                    )
                )
                net_realized += realized
                positions[ticker] -= shares
                remaining -= shares * price

        proceeds_total = sum(t.proceeds for t in trades)
        if underweight and proceeds_total > 0:
            total_need = sum(underweight.values())
            for cls, need in sorted(underweight.items()):
                cls_tickers = [h["ticker"] for h in self.holdings if h["asset_class"] == cls]
                if not cls_tickers:
                    continue
                class_value_now = sum(positions[t] * self.prices[t] for t in cls_tickers)
                budget = proceeds_total * (need / total_need) if total_need else 0.0
                for ticker in cls_tickers:
                    weight = (positions[ticker] * self.prices[ticker] / class_value_now) if class_value_now else 1.0 / len(cls_tickers)
                    buy_value = budget * weight
                    if buy_value <= 0.01:
                        continue
                    shares = round(buy_value / self.prices[ticker], 4)
                    trades.append(
                        Trade(
                            ticker=ticker,
                            side="buy",
                            shares=shares,
                            price=self.prices[ticker],
                            proceeds=shares * self.prices[ticker],
                        )
                    )
                    positions[ticker] += shares

        class_values_after = self._class_values(positions)
        drift_after = self._drift(class_values_after, total, targets)
        savings = round(max(0.0, -net_realized) * TAX_RATE, 2)

        return OracleResult(
            trades=trades,
            tax_savings_estimate=savings,
            drift_reduction=round((drift_before - drift_after) * 100, 2),
            net_realized_gain_loss=round(net_realized, 2),
            drift_before=round(drift_before * 100, 2),
            drift_after=round(drift_after * 100, 2),
        )
