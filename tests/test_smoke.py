"""Smoke tests: the deterministic pieces genuinely execute, repeat, and
fail in the ways the article says they should.

Run from the project root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.compliance import run_compliance_check
from adapters.graphql_client import fetch_market_data
from adapters.tax import run_tax_optimization
from pipeline import DEMO_REQUEST, build_runtime


class MarketDataTest(unittest.TestCase):
    def test_contract_fields_present(self):
        data = json.loads(fetch_market_data("AAPL,MSFT,BND"))
        self.assertEqual(set(data), {"AAPL", "MSFT", "BND"})
        for row in data.values():
            self.assertEqual(set(row), {"price", "volatility", "sentiment"})

    def test_article_example_values(self):
        data = json.loads(fetch_market_data("AAPL,MSFT"))
        self.assertEqual(data["AAPL"]["price"], 175.20)
        self.assertEqual(data["MSFT"]["price"], 420.50)


class TaxTest(unittest.TestCase):
    def _payload(self):
        return json.dumps(
            {
                "holdings": DEMO_REQUEST["holdings"],
                "prices": {"AAPL": 175.20, "MSFT": 420.50, "BND": 72.40},
                "targets": {"stock": 0.6, "bond": 0.4},
            }
        )

    def test_repeatable(self):
        first = run_tax_optimization(self._payload())
        second = run_tax_optimization(self._payload())
        self.assertEqual(first, second)

    def test_harvests_the_loss_lot_first(self):
        result = json.loads(run_tax_optimization(self._payload()))
        sells = [t for t in result["recommended_trades"] if t["side"] == "sell"]
        self.assertTrue(sells)
        self.assertEqual(sells[0]["ticker"], "AAPL")
        self.assertEqual(sells[0]["lot_cost_basis"], 190.00)
        self.assertLess(result["net_realized_gain_loss"], 0)
        self.assertGreater(result["tax_savings_estimate"], 0)
        self.assertGreater(result["drift_reduction"], 0)


class ComplianceTest(unittest.TestCase):
    def _run(self, trades, recent_sales=None, as_of="2026-11-24"):
        payload = {
            "trades": trades,
            "holdings": DEMO_REQUEST["holdings"],
            "prices": {"AAPL": 175.20, "MSFT": 420.50, "BND": 72.40},
            "recent_sales": recent_sales or [],
            "as_of": as_of,
        }
        return json.loads(run_compliance_check(json.dumps(payload)))

    def test_demo_trades_pass(self):
        tax = json.loads(
            run_tax_optimization(
                json.dumps(
                    {
                        "holdings": DEMO_REQUEST["holdings"],
                        "prices": {"AAPL": 175.20, "MSFT": 420.50, "BND": 72.40},
                        "targets": {"stock": 0.6, "bond": 0.4},
                    }
                )
            )
        )
        result = self._run(tax["recommended_trades"])
        self.assertEqual(result["status"], "pass", result["reasons"])

    def test_wash_sale_fails(self):
        trades = [
            {"ticker": "AAPL", "side": "buy", "shares": 10, "price": 175.20,
             "proceeds": 1752.00, "realized_gain_loss": 0.0, "lot_cost_basis": None}
        ]
        recent = [{"ticker": "AAPL", "date": "2026-11-10", "realized_gain_loss": -500.0}]
        result = self._run(trades, recent_sales=recent)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("wash" in r.lower() or "AAPL" in r for r in result["reasons"]))

    def test_concentration_fails(self):
        trades = [
            {"ticker": "MSFT", "side": "buy", "shares": 60, "price": 420.50,
             "proceeds": 25230.00, "realized_gain_loss": 0.0, "lot_cost_basis": None}
        ]
        result = self._run(trades)
        self.assertEqual(result["status"], "fail")


class PipelineTest(unittest.TestCase):
    def test_demo_run_end_to_end(self):
        runtime, tracker = build_runtime(trace_path="runs/test-trace.jsonl")
        result = runtime.run("rebalance", DEMO_REQUEST)
        self.assertEqual(result["status"], "complete")
        self.assertIn("Rebalance plan:", result["plan"])
        self.assertEqual(result["compliance"]["status"], "pass")
        # The deterministic steps spent no tokens, as the article reports.
        self.assertEqual(tracker.by_name.get("tax.optimize", 0), 0)
        self.assertEqual(tracker.by_name.get("compliance.check", 0), 0)
        self.assertGreater(tracker.total_tokens, 0)


if __name__ == "__main__":
    unittest.main()
