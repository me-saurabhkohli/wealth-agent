"""Oracle: deterministic, tax-aware portfolio optimization.

The article calls the tax library "Oracle" and imports it from a private
service package. That package is not public, so this module is a
self-contained implementation with the same job and the same output shape:
given holdings with tax lots, current prices, and a target allocation, it
produces tax-lot aware trade recommendations, a tax savings estimate, and
the drift reduction. Pure Python, no model calls, no randomness. Every
date comes from the request (as_of and the lot acquisition dates), never
from the clock, so the same inputs always produce the same trades.

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

Wash sale awareness: a loss is disallowed when substantially identical
shares were bought within 30 days before or after the sale. This
optimizer therefore runs its lot walk twice. The first walk is the plan
it would make ignoring the rule. If that plan would leave recently
acquired shares of a ticker in the portfolio while also selling other
lots of the same ticker at a loss, the retained shares are replacement
shares and the loss would be disallowed, so the second walk skips those
loss lots entirely (the demo skips the whole lot rather than splitting
the disallowance share by share) and sells the next lots instead. The
same skip applies when a recent sale of the ticker at a loss sits inside
the window and the plan buys the ticker back. Skipped lots are reported
on the result, with the reason, instead of vanishing silently. If the
first walk sells every share of the ticker, no replacement shares
remain, nothing is disallowed, and nothing is skipped.

Short and long term: each sale is classified from its lot. A lot held
more than one year (acquired more than 365 days before as_of) is long
term; anything shorter is short term. The classification is reported per
trade and summed in gain_loss_breakdown. The savings estimate keeps the
single documented TAX_RATE assumption for the headline number; the
breakdown is reporting, not a second estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Assumed combined marginal rate used only to turn a harvested loss into a
# savings estimate. It is an assumption of the demo, not tax advice.
TAX_RATE = 0.25

WASH_WINDOW_DAYS = 30
LONG_TERM_DAYS = 365


@dataclass
class Trade:
    ticker: str
    side: str            # "sell" or "buy"
    shares: float
    price: float
    proceeds: float      # shares * price, positive
    realized_gain_loss: float = 0.0   # sells only; buys are 0.0
    lot_cost_basis: float | None = None  # per share, sells only
    term: str | None = None  # "short" or "long" for sells; None for buys

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "shares": round(self.shares, 2),
            "price": round(self.price, 2),
            "proceeds": round(self.proceeds, 2),
            "realized_gain_loss": round(self.realized_gain_loss, 2),
            "lot_cost_basis": None if self.lot_cost_basis is None else round(self.lot_cost_basis, 2),
            "term": self.term,
        }


@dataclass
class OracleResult:
    trades: list[Trade] = field(default_factory=list)
    tax_savings_estimate: float = 0.0
    drift_reduction: float = 0.0
    net_realized_gain_loss: float = 0.0
    drift_before: float = 0.0
    drift_after: float = 0.0
    gain_loss_breakdown: dict = field(
        default_factory=lambda: {"short_term_net": 0.0, "long_term_net": 0.0}
    )
    skipped_lots: list[dict] = field(default_factory=list)


class OracleStrategy:
    """Strategy settings, mirroring the article's OracleStrategy shape."""

    def __init__(self, targets: dict[str, float], optimization_type: str = "TAX_AWARE") -> None:
        total = sum(targets.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Target allocation must sum to 1.0, got {total}")
        self.targets = dict(targets)
        self.optimization_type = optimization_type


class Oracle:
    """The optimizer. Construct with a strategy, holdings, prices, and
    the request context the tax rules need (recent sales and as_of), then
    call optimize()."""

    def __init__(
        self,
        strategy: OracleStrategy,
        holdings: list[dict],
        prices: dict[str, float],
        recent_sales: list[dict] | None = None,
        as_of: str = "2026-11-24",
    ) -> None:
        self.strategy = strategy
        self.holdings = holdings
        self.prices = prices
        self.recent_sales = recent_sales or []
        self.as_of = date.fromisoformat(as_of)

    # -- lot helpers ----------------------------------------------------

    def _fresh_lots(self) -> list[dict]:
        """Lot records for one walk. Each walk starts from a clean copy so
        the wash sale check can run a second walk without the first walk's
        sales leaking into it."""
        lots = []
        for holding in self.holdings:
            for lot in holding.get("lots", []):
                shares = float(lot["shares"])
                lots.append(
                    {
                        "ticker": holding["ticker"],
                        "asset_class": holding["asset_class"],
                        "basis": float(lot["cost_basis"]),
                        "acquired": lot["acquired"],
                        "shares": shares,
                        "remaining": shares,
                    }
                )
        return lots

    def _term(self, acquired: str) -> str:
        held_days = (self.as_of - date.fromisoformat(acquired)).days
        return "long" if held_days > LONG_TERM_DAYS else "short"

    def _is_recent(self, acquired: str) -> bool:
        days_ago = (self.as_of - date.fromisoformat(acquired)).days
        return 0 <= days_ago <= WASH_WINDOW_DAYS

    # -- the walk ---------------------------------------------------------

    def _walk(self, lots: list[dict], skip_loss_tickers: set[str]):
        """One pass of the selling and buying plan. Returns the trades,
        the net realized gain or loss, the per-term breakdown, and the
        positions after the trades. Lots in skip_loss_tickers keep their
        loss lots out of the candidate list; their gain lots still sell."""
        targets = self.strategy.targets
        positions = {h["ticker"]: float(h["shares"]) for h in self.holdings}
        total = sum(positions[t] * self.prices[t] for t in positions)

        trades: list[Trade] = []
        net_realized = 0.0
        breakdown = {"short_term_net": 0.0, "long_term_net": 0.0}

        class_values = self._class_values(positions)
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
            candidates = []
            for lot in lots:
                if lot["asset_class"] != cls:
                    continue
                gain_per_share = self.prices[lot["ticker"]] - lot["basis"]
                if gain_per_share < 0 and lot["ticker"] in skip_loss_tickers:
                    continue
                candidates.append((gain_per_share, lot["ticker"], lot["basis"], lot))
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))

            for gain_per_share, ticker, basis, lot in candidates:
                if remaining <= 0.01:
                    break
                price = self.prices[ticker]
                available_value = min(lot["remaining"], positions[ticker]) * price
                sell_value = min(remaining, available_value)
                shares = round(sell_value / price, 4)
                if shares <= 0:
                    continue
                realized = shares * gain_per_share
                term = self._term(lot["acquired"])
                trades.append(
                    Trade(
                        ticker=ticker,
                        side="sell",
                        shares=shares,
                        price=price,
                        proceeds=shares * price,
                        realized_gain_loss=realized,
                        lot_cost_basis=basis,
                        term=term,
                    )
                )
                net_realized += realized
                breakdown[f"{term}_term_net"] += realized
                positions[ticker] -= shares
                lot["remaining"] -= shares
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

        return trades, net_realized, breakdown, positions

    # -- wash sale exposure ----------------------------------------------

    def _exposed_tickers(self, lots: list[dict], trades: list[Trade]) -> set[str]:
        """Tickers whose loss sales in this walk would be disallowed.

        Two sources of replacement shares: same-ticker lots acquired
        within the wash window that the walk leaves in the portfolio, and
        a buy of the ticker in the same plan while a loss sale of it sits
        in recent_sales inside the window."""
        sold_at_loss = {
            t.ticker for t in trades if t.side == "sell" and t.realized_gain_loss < 0
        }
        if not sold_at_loss:
            return set()
        bought = {t.ticker for t in trades if t.side == "buy"}
        recent_loss_sales = {
            sale["ticker"]
            for sale in self.recent_sales
            if float(sale.get("realized_gain_loss", 0)) < 0
            and 0 <= (self.as_of - date.fromisoformat(sale["date"])).days <= WASH_WINDOW_DAYS
        }
        exposed = set()
        for ticker in sold_at_loss:
            retained_recent = sum(
                lot["remaining"]
                for lot in lots
                if lot["ticker"] == ticker and lot["remaining"] > 0 and self._is_recent(lot["acquired"])
            )
            if retained_recent > 0:
                exposed.add(ticker)
            elif ticker in recent_loss_sales and ticker in bought:
                exposed.add(ticker)
        return exposed

    def _skip_reason(self, ticker: str, lots: list[dict]) -> str:
        retained = [
            lot for lot in lots
            if lot["ticker"] == ticker and lot["remaining"] > 0 and self._is_recent(lot["acquired"])
        ]
        if retained:
            shares = sum(lot["remaining"] for lot in retained)
            dates = ", ".join(sorted({lot["acquired"] for lot in retained}))
            return (
                f"shares of {ticker} acquired on {dates} ({shares:.2f} shares) stay in the "
                f"portfolio inside the {WASH_WINDOW_DAYS} day wash sale window, so a loss "
                f"sale of {ticker} would be disallowed"
            )
        return (
            f"a loss sale of {ticker} sits in recent sales inside the "
            f"{WASH_WINDOW_DAYS} day wash sale window and the plan buys {ticker} back"
        )

    # -- main entry -------------------------------------------------------

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

        lots = self._fresh_lots()
        trades, net_realized, breakdown, positions = self._walk(lots, skip_loss_tickers=set())

        exposed = self._exposed_tickers(lots, trades)
        skipped_lots: list[dict] = []
        if exposed:
            reason_by_ticker = {ticker: self._skip_reason(ticker, lots) for ticker in exposed}
            for lot in lots:
                if lot["ticker"] in exposed and self.prices[lot["ticker"]] - lot["basis"] < 0:
                    skipped_lots.append(
                        {
                            "ticker": lot["ticker"],
                            "shares": round(lot["shares"], 2),
                            "lot_cost_basis": round(lot["basis"], 2),
                            "acquired": lot["acquired"],
                            "reason": reason_by_ticker[lot["ticker"]],
                        }
                    )
            skipped_lots.sort(key=lambda item: (item["ticker"], item["lot_cost_basis"]))
            lots = self._fresh_lots()
            trades, net_realized, breakdown, positions = self._walk(
                lots, skip_loss_tickers=exposed
            )

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
            gain_loss_breakdown={
                "short_term_net": round(breakdown["short_term_net"], 2),
                "long_term_net": round(breakdown["long_term_net"], 2),
            },
            skipped_lots=skipped_lots,
        )
