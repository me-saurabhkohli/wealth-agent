"""Generate the synthetic client portfolios under data/.

The portfolios are curated templates, not random noise: each one exists
to exercise a specific path through the demo (a clean harvest, a recent
sale inside the wash window, a loss lot the optimizer has to skip, short
term lots, a portfolio the compliance gate blocks). A fixed seed drives
the only variation, the acquisition dates, and each date is drawn from a
window that cannot change its classification: long term lots are drawn
400 to 1100 days before as_of, short term lots 60 to 340 days, recent
lots 3 to 28 days. Same seed, same files, byte for byte.

After writing, the generator checks its own work: every portfolio runs
through the real tax and compliance adapters at the mock prices, and the
generator fails loudly if a client designed to pass gets blocked, or the
blocked client passes.

Usage: python data/generate_synthetic.py [output_dir]
"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from adapters.compliance import run_compliance_check
from adapters.graphql_client import MOCK_MARKET_DATA
from adapters.tax import run_tax_optimization

SEED = 20261124
AS_OF = date(2026, 11, 24)

LONG = (400, 1100)    # days before as_of: safely over the one year line
SHORT = (60, 340)     # days before as_of: under one year, outside the wash window
RECENT = (3, 28)      # days before as_of: inside the 30 day wash window


def _acquired(rng: random.Random, window: tuple[int, int]) -> str:
    return (AS_OF - timedelta(days=rng.randint(*window))).isoformat()


def _lot(rng, shares, basis, window):
    return {"shares": shares, "cost_basis": basis, "acquired": _acquired(rng, window)}


def _holding(ticker, asset_class, lots):
    return {
        "ticker": ticker,
        "asset_class": asset_class,
        "shares": sum(lot["shares"] for lot in lots),
        "lots": lots,
    }


def build_clients() -> list[dict]:
    rng = random.Random(SEED)
    clients = []

    # SYN001: a clean harvest. Stock heavy, one deep AAPL loss lot, target
    # reachable, every rule passes.
    clients.append(
        {
            "client_id": "SYN001",
            "goal": "Rebalance client SYN001 for tax efficiency, target 60/40",
            "targets": {"stock": 0.6, "bond": 0.4},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("AAPL", "stock", [_lot(rng, 40, 150.00, LONG), _lot(rng, 20, 205.00, LONG)]),
                _holding("MSFT", "stock", [_lot(rng, 30, 350.00, LONG)]),
                _holding("NVDA", "stock", [_lot(rng, 50, 95.00, LONG)]),
                _holding("BND", "bond", [_lot(rng, 100, 68.00, LONG)]),
            ],
            "recent_sales": [],
        }
    )

    # SYN002: a loss sale of MSFT sits in recent_sales inside the wash
    # window. The plan sells more MSFT at a loss and buys bonds, so no buy
    # conflicts and the run passes. The tension is in the data, not the
    # outcome.
    clients.append(
        {
            "client_id": "SYN002",
            "goal": "Rebalance client SYN002 for tax efficiency, target 65/35",
            "targets": {"stock": 0.65, "bond": 0.35},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("MSFT", "stock", [_lot(rng, 25, 470.00, LONG), _lot(rng, 15, 390.00, LONG)]),
                _holding("AAPL", "stock", [_lot(rng, 50, 140.00, LONG)]),
                _holding("AGG", "bond", [_lot(rng, 60, 95.00, LONG)]),
            ],
            "recent_sales": [
                {
                    "ticker": "MSFT",
                    "date": _acquired(rng, (10, 25)),
                    "realized_gain_loss": -1800.00,
                }
            ],
        }
    )

    # SYN003: the wash sale skip. AAPL has a deep loss lot and a small lot
    # acquired inside the wash window that the rebalance leaves in place,
    # so the optimizer skips the loss lot and sells gains instead. The
    # skipped lot shows up in the tax result.
    clients.append(
        {
            "client_id": "SYN003",
            "goal": "Rebalance client SYN003 for tax efficiency, target 60/40",
            "targets": {"stock": 0.6, "bond": 0.4},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("AAPL", "stock", [_lot(rng, 95, 190.00, LONG), _lot(rng, 20, 170.00, RECENT)]),
                _holding("MSFT", "stock", [_lot(rng, 20, 300.00, LONG)]),
                _holding("BND", "bond", [_lot(rng, 80, 70.00, LONG)]),
            ],
            "recent_sales": [],
        }
    )

    # SYN004: bond heavy with a harvestable loss in AGG, plus short term
    # lots in the portfolio so the classification reporting has something
    # to show.
    clients.append(
        {
            "client_id": "SYN004",
            "goal": "Rebalance client SYN004 for tax efficiency, target 50/50",
            "targets": {"stock": 0.5, "bond": 0.5},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("AGG", "bond", [_lot(rng, 120, 104.00, LONG), _lot(rng, 80, 92.00, SHORT)]),
                _holding("BND", "bond", [_lot(rng, 50, 70.00, LONG)]),
                _holding("VTI", "stock", [_lot(rng, 40, 240.00, LONG)]),
                _holding("QQQ", "stock", [_lot(rng, 10, 480.00, SHORT)]),
            ],
            "recent_sales": [],
        }
    )

    # SYN005: a newer portfolio. One NVDA lot was acquired inside the wash
    # window but sits at a gain, and the harvest comes from an AMZN loss
    # lot in a different ticker, so the recent lot changes nothing and the
    # run passes.
    clients.append(
        {
            "client_id": "SYN005",
            "goal": "Rebalance client SYN005 for tax efficiency, target 70/30",
            "targets": {"stock": 0.7, "bond": 0.3},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("NVDA", "stock", [_lot(rng, 40, 100.00, SHORT), _lot(rng, 20, 128.00, RECENT)]),
                _holding("AMZN", "stock", [_lot(rng, 40, 210.00, LONG)]),
                _holding("BND", "bond", [_lot(rng, 40, 71.00, LONG)]),
            ],
            "recent_sales": [],
        }
    )

    # DEMO_BLOCKED: one stock and almost nothing else. The optimizer can
    # reach the 60/40 target, but MSFT would still be 60 percent of the
    # portfolio afterwards, over the 50 percent concentration limit, so
    # the compliance gate blocks the run and no plan is produced.
    clients.append(
        {
            "client_id": "DEMO_BLOCKED",
            "goal": "Rebalance client DEMO_BLOCKED for tax efficiency, target 60/40",
            "targets": {"stock": 0.6, "bond": 0.4},
            "as_of": AS_OF.isoformat(),
            "holdings": [
                _holding("MSFT", "stock", [_lot(rng, 60, 300.00, LONG), _lot(rng, 40, 460.00, LONG)]),
                _holding("BND", "bond", [_lot(rng, 10, 70.00, LONG)]),
            ],
            "recent_sales": [],
        }
    )

    return clients


def expected_status(client_id: str) -> str:
    return "fail" if client_id == "DEMO_BLOCKED" else "pass"


def check_client(client: dict) -> str:
    """Run one portfolio through the real tax and compliance adapters at
    the mock prices and return the compliance status."""
    tickers = [h["ticker"] for h in client["holdings"]]
    prices = {t: MOCK_MARKET_DATA[t]["price"] for t in tickers}
    tax_result = json.loads(
        run_tax_optimization(
            json.dumps(
                {
                    "holdings": client["holdings"],
                    "prices": prices,
                    "targets": client["targets"],
                    "recent_sales": client.get("recent_sales", []),
                    "as_of": client["as_of"],
                }
            )
        )
    )
    compliance = json.loads(
        run_compliance_check(
            json.dumps(
                {
                    "trades": tax_result["recommended_trades"],
                    "holdings": client["holdings"],
                    "prices": prices,
                    "targets": client["targets"],
                    "recent_sales": client.get("recent_sales", []),
                    "as_of": client["as_of"],
                }
            )
        )
    )
    return compliance["status"]


def generate(out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for client in build_clients():
        status = check_client(client)
        expected = expected_status(client["client_id"])
        if status != expected:
            raise SystemExit(
                f"Generator design error: {client['client_id']} was designed to "
                f"{'be blocked' if expected == 'fail' else 'pass'} but compliance says {status}"
            )
        path = os.path.join(out_dir, f"{client['client_id']}.json")
        with open(path, "w") as fh:
            json.dump(client, fh, indent=2)
            fh.write("\n")
        written.append(path)
    return written


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    written = generate(out_dir)
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
