"""Tests for the v2 upgrade: synthetic data, contract schemas, the fuller
Oracle, and the compliance rules engine.

Run from the project root:  python -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from adapters.compliance import RuleSetDecision, run_compliance_check
from adapters.graphql_client import GRAPHQL_QUERY, fetch_market_data
from adapters.oracle import Oracle, OracleStrategy
from adapters.tax import run_tax_optimization
from contracts import SchemaValidationError, validate
from pipeline import DEMO_REQUEST, build_runtime
from runtime import ContractError

PRICES = {"AAPL": 175.20, "MSFT": 420.50, "BND": 72.40, "AGG": 98.50}


def _load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _failed_rules(result):
    return {check["rule"] for check in result["checks"] if check["status"] == "fail"}


class GeneratorTest(unittest.TestCase):
    def test_deterministic_and_shaped(self):
        generator = _load_module("generate_synthetic", "data/generate_synthetic.py")
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            generator.generate(first)
            generator.generate(second)
            names_first = sorted(os.listdir(first))
            self.assertEqual(names_first, sorted(os.listdir(second)))
            self.assertGreaterEqual(len(names_first), 6)
            self.assertIn("DEMO_BLOCKED.json", names_first)
            for name in names_first:
                with open(os.path.join(first, name), "rb") as fh:
                    bytes_first = fh.read()
                with open(os.path.join(second, name), "rb") as fh:
                    self.assertEqual(bytes_first, fh.read(), name)
                client = json.loads(bytes_first)
                for key in ("client_id", "goal", "targets", "as_of", "holdings", "recent_sales"):
                    self.assertIn(key, client, f"{name} missing {key}")
                for holding in client["holdings"]:
                    self.assertIn("lots", holding)


class ContractSchemaTest(unittest.TestCase):
    def test_valid_payloads_pass(self):
        validate("MarketData", {"AAPL": {"price": 175.20, "volatility": 0.18, "sentiment": 0.7}})
        validate(
            "TaxResult",
            {
                "recommended_trades": [
                    {"ticker": "AAPL", "side": "sell", "shares": 1, "price": 175.20,
                     "proceeds": 175.20, "realized_gain_loss": -14.80,
                     "lot_cost_basis": 190.00, "term": "long"}
                ],
                "tax_savings_estimate": 3.70,
                "drift_reduction": 1.0,
            },
        )
        validate(
            "ComplianceResult",
            {"status": "pass",
             "checks": [{"rule": "cash_residual", "status": "pass", "detail": "ok"}],
             "reasons": []},
        )

    def test_malformed_market_payload_caught(self):
        with self.assertRaises(SchemaValidationError):
            validate("MarketData", {"AAPL": {"price": "high", "volatility": 0.18, "sentiment": 0.7}})
        with self.assertRaises(SchemaValidationError):
            validate("MarketData", {"AAPL": {"price": 175.20, "volatility": 0.18}})
        with self.assertRaises(SchemaValidationError):
            validate("MarketData", {})

    def test_malformed_tax_and_compliance_caught(self):
        with self.assertRaises(SchemaValidationError):
            validate("TaxResult", {"recommended_trades": [], "tax_savings_estimate": 0})
        with self.assertRaises(SchemaValidationError):
            validate(
                "TaxResult",
                {"recommended_trades": [
                    {"ticker": "AAPL", "side": "sell", "shares": 1, "price": 1,
                     "proceeds": 1, "realized_gain_loss": 0, "lot_cost_basis": None}
                 ], "tax_savings_estimate": 0, "drift_reduction": 0},
            )
        with self.assertRaises(SchemaValidationError):
            validate(
                "ComplianceResult",
                {"status": "pass",
                 "checks": [{"rule": "not_a_rule", "status": "pass", "detail": "ok"}],
                 "reasons": []},
            )

    def test_runtime_rejects_malformed_adapter_output(self):
        runtime, _tracker = build_runtime(trace_path="runs/test-contract.jsonl")
        runtime.adapters["tax.optimize"] = lambda payload: "{}"
        with self.assertRaises(ContractError):
            runtime.run("rebalance", DEMO_REQUEST)


class RulesEngineTest(unittest.TestCase):
    def _evaluate(self, trades, holdings, targets, recent_sales=None, as_of="2026-11-24"):
        payload = {
            "trades": trades,
            "holdings": holdings,
            "prices": PRICES,
            "targets": targets,
            "recent_sales": recent_sales or [],
            "as_of": as_of,
        }
        return RuleSetDecision().evaluate(payload)

    def _holding(self, ticker, shares):
        return {"ticker": ticker, "asset_class": "stock" if ticker != "BND" and ticker != "AGG" else "bond",
                "shares": shares, "lots": []}

    def test_concentration_fails_independently(self):
        holdings = [self._holding("MSFT", 50), self._holding("BND", 200)]
        result = self._evaluate([], holdings, {"stock": 0.592, "bond": 0.408})
        self.assertEqual(_failed_rules(result), {"concentration_limit"})

    def test_wash_sale_fails_independently(self):
        holdings = [self._holding("AAPL", 90), self._holding("MSFT", 20), self._holding("BND", 130)]
        trades = [
            {"ticker": "BND", "side": "sell", "shares": 5, "price": 72.40,
             "proceeds": 362.00, "realized_gain_loss": 0.0, "lot_cost_basis": 70.00, "term": "long"},
            {"ticker": "AAPL", "side": "buy", "shares": 2, "price": 175.20,
             "proceeds": 350.40, "realized_gain_loss": 0.0, "lot_cost_basis": None, "term": None},
        ]
        recent = [{"ticker": "AAPL", "date": "2026-11-10", "realized_gain_loss": -500.0}]
        result = self._evaluate(trades, holdings, {"stock": 0.73, "bond": 0.27}, recent_sales=recent)
        self.assertEqual(_failed_rules(result), {"wash_sale_window"})

    def test_drift_fails_independently(self):
        holdings = [self._holding("AAPL", 50), self._holding("MSFT", 20), self._holding("BND", 121)]
        result = self._evaluate([], holdings, {"stock": 0.6, "bond": 0.4})
        self.assertEqual(_failed_rules(result), {"drift_tolerance"})

    def test_sell_within_holdings_fails_independently(self):
        # The sale is deliberately impossible (20 shares sold, 10 held);
        # the other rules are fed numbers that keep them green so only
        # this rule can fail.
        holdings = [self._holding("AAPL", 10), self._holding("MSFT", 10),
                    self._holding("BND", 100), self._holding("AGG", 74)]
        trades = [
            {"ticker": "AAPL", "side": "sell", "shares": 20, "price": 175.20,
             "proceeds": 3504.00, "realized_gain_loss": 100.0, "lot_cost_basis": 120.00, "term": "long"},
            {"ticker": "BND", "side": "buy", "shares": 24.19, "price": 72.40,
             "proceeds": 1751.36, "realized_gain_loss": 0.0, "lot_cost_basis": None, "term": None},
            {"ticker": "AGG", "side": "buy", "shares": 17.78, "price": 98.50,
             "proceeds": 1751.33, "realized_gain_loss": 0.0, "lot_cost_basis": None, "term": None},
        ]
        result = self._evaluate(trades, holdings, {"stock": 0.189, "bond": 0.811})
        self.assertEqual(_failed_rules(result), {"sell_within_holdings"})

    def test_cash_residual_fails_independently(self):
        trades = [
            {"ticker": "BND", "side": "buy", "shares": 10, "price": 72.40,
             "proceeds": 724.00, "realized_gain_loss": 0.0, "lot_cost_basis": None, "term": None}
        ]
        result = self._evaluate(trades, DEMO_REQUEST["holdings"], {"stock": 0.855, "bond": 0.145})
        self.assertEqual(_failed_rules(result), {"cash_residual"})

    def test_demo_payload_passes_every_rule(self):
        tax = json.loads(
            run_tax_optimization(
                json.dumps(
                    {"holdings": DEMO_REQUEST["holdings"], "prices": PRICES,
                     "targets": {"stock": 0.6, "bond": 0.4},
                     "recent_sales": [], "as_of": "2026-11-24"}
                )
            )
        )
        payload = {
            "trades": tax["recommended_trades"],
            "holdings": DEMO_REQUEST["holdings"],
            "prices": PRICES,
            "targets": {"stock": 0.6, "bond": 0.4},
            "recent_sales": [],
            "as_of": "2026-11-24",
        }
        result = json.loads(run_compliance_check(json.dumps(payload)))
        self.assertEqual(result["status"], "pass", result["reasons"])
        self.assertEqual(
            {check["rule"] for check in result["checks"]},
            {"concentration_limit", "wash_sale_window", "drift_tolerance",
             "sell_within_holdings", "cash_residual"},
        )


class OracleTest(unittest.TestCase):
    def _oracle(self, holdings, prices, targets, recent_sales=None, as_of="2026-11-24"):
        return Oracle(
            OracleStrategy(targets),
            holdings,
            prices,
            recent_sales=recent_sales or [],
            as_of=as_of,
        )

    def test_wash_sale_lot_is_skipped(self):
        holdings = [
            {"ticker": "AAPL", "asset_class": "stock", "shares": 120,
             "lots": [{"shares": 100, "cost_basis": 190.00, "acquired": "2023-01-15"},
                      {"shares": 20, "cost_basis": 170.00, "acquired": "2026-11-10"}]},
            {"ticker": "BND", "asset_class": "bond", "shares": 80,
             "lots": [{"shares": 80, "cost_basis": 70.00, "acquired": "2024-01-15"}]},
        ]
        result = self._oracle(holdings, {"AAPL": 175.20, "BND": 72.40}, {"stock": 0.6, "bond": 0.4}).optimize()
        self.assertTrue(result.skipped_lots)
        self.assertEqual(result.skipped_lots[0]["ticker"], "AAPL")
        self.assertEqual(result.skipped_lots[0]["lot_cost_basis"], 190.00)
        sells = [t for t in result.trades if t.side == "sell"]
        self.assertFalse(any(t.lot_cost_basis == 190.00 for t in sells))

    def test_same_lot_harvests_without_the_recent_lot(self):
        holdings = [
            {"ticker": "AAPL", "asset_class": "stock", "shares": 100,
             "lots": [{"shares": 100, "cost_basis": 190.00, "acquired": "2023-01-15"}]},
            {"ticker": "BND", "asset_class": "bond", "shares": 80,
             "lots": [{"shares": 80, "cost_basis": 70.00, "acquired": "2024-01-15"}]},
        ]
        result = self._oracle(holdings, {"AAPL": 175.20, "BND": 72.40}, {"stock": 0.6, "bond": 0.4}).optimize()
        self.assertEqual(result.skipped_lots, [])
        sells = [t for t in result.trades if t.side == "sell"]
        self.assertTrue(any(t.lot_cost_basis == 190.00 for t in sells))

    def test_full_exit_is_not_skipped(self):
        # Selling the whole position leaves no replacement shares behind,
        # so the recently acquired lot does not block the harvest.
        holdings = [
            {"ticker": "AAPL", "asset_class": "stock", "shares": 120,
             "lots": [{"shares": 100, "cost_basis": 190.00, "acquired": "2023-01-15"},
                      {"shares": 20, "cost_basis": 170.00, "acquired": "2026-11-10"}]},
            {"ticker": "BND", "asset_class": "bond", "shares": 400,
             "lots": [{"shares": 400, "cost_basis": 70.00, "acquired": "2024-01-15"}]},
        ]
        result = self._oracle(holdings, {"AAPL": 175.20, "BND": 72.40}, {"stock": 0.0, "bond": 1.0}).optimize()
        self.assertEqual(result.skipped_lots, [])
        sells = [t for t in result.trades if t.side == "sell" and t.ticker == "AAPL"]
        self.assertEqual(sum(t.shares for t in sells), 120.0)

    def test_short_long_classification(self):
        holdings = [
            {"ticker": "AAA", "asset_class": "stock", "shares": 100,
             "lots": [{"shares": 50, "cost_basis": 90.00, "acquired": "2026-06-01"},
                      {"shares": 50, "cost_basis": 80.00, "acquired": "2023-01-01"}]},
            {"ticker": "BBB", "asset_class": "bond", "shares": 100,
             "lots": [{"shares": 100, "cost_basis": 50.00, "acquired": "2023-01-01"}]},
        ]
        result = self._oracle(holdings, {"AAA": 100.00, "BBB": 50.00}, {"stock": 0.3, "bond": 0.7}).optimize()
        sells = [t for t in result.trades if t.side == "sell"]
        self.assertEqual({t.term for t in sells}, {"short", "long"})
        self.assertEqual(result.gain_loss_breakdown["short_term_net"], 500.00)
        self.assertEqual(result.gain_loss_breakdown["long_term_net"], 100.00)
        self.assertEqual(
            round(result.gain_loss_breakdown["short_term_net"] + result.gain_loss_breakdown["long_term_net"], 2),
            result.net_realized_gain_loss,
        )


class PublishedNumbersTest(unittest.TestCase):
    def test_abc123_tax_numbers_unchanged(self):
        result = json.loads(
            run_tax_optimization(
                json.dumps(
                    {"holdings": DEMO_REQUEST["holdings"],
                     "prices": {"AAPL": 175.20, "MSFT": 420.50, "BND": 72.40},
                     "targets": {"stock": 0.6, "bond": 0.4},
                     "recent_sales": [], "as_of": "2026-11-24"}
                )
            )
        )
        sells = [t for t in result["recommended_trades"] if t["side"] == "sell"]
        buys = [t for t in result["recommended_trades"] if t["side"] == "buy"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["ticker"], "AAPL")
        self.assertEqual(sells[0]["shares"], 68.17)
        self.assertEqual(sells[0]["lot_cost_basis"], 190.00)
        self.assertEqual(sells[0]["realized_gain_loss"], -1008.87)
        self.assertEqual(sells[0]["term"], "long")
        self.assertEqual(buys[0]["ticker"], "BND")
        self.assertEqual(buys[0]["shares"], 164.96)
        self.assertEqual(result["tax_savings_estimate"], 252.22)
        self.assertEqual(result["drift_reduction"], 53.87)
        self.assertEqual(result["drift_after_pct"], 0.0)
        self.assertEqual(result["gain_loss_breakdown"]["long_term_net"], -1008.87)
        self.assertEqual(result["gain_loss_breakdown"]["short_term_net"], 0.0)
        self.assertEqual(result["skipped_lots"], [])

    def test_abc123_pipeline_tokens_unchanged(self):
        runtime, tracker = build_runtime(trace_path="runs/test-v2-abc123.jsonl")
        result = runtime.run("rebalance", DEMO_REQUEST)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["tokens_used"], 168)
        self.assertEqual(tracker.total_tokens, 168)

    def test_blocked_client_blocks_through_the_pipeline(self):
        with open(os.path.join(ROOT, "data", "DEMO_BLOCKED.json")) as fh:
            request = json.load(fh)
        runtime, _tracker = build_runtime(trace_path="runs/test-v2-blocked.jsonl")
        result = runtime.run("rebalance", request)
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["plan"])
        self.assertTrue(any("60.0 percent" in r for r in result["compliance"]["reasons"]))


class MockGraphQLServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = _load_module("mock_graphql_server", "tools/mock_graphql_server.py")

    def test_approved_query_returns_contract_rows(self):
        result = self.server_module.handle_graphql(
            {"query": GRAPHQL_QUERY, "variables": {"tickers": ["AAPL", "BND"]}}
        )
        self.assertIn("data", result)
        rows = {row["ticker"]: row for row in result["data"]["marketData"]}
        self.assertEqual(rows["AAPL"]["price"], 175.20)
        self.assertEqual(rows["BND"]["volatility30d"], 0.05)

    def test_invented_field_is_rejected_with_a_name(self):
        tampered = GRAPHQL_QUERY.replace("sentiment", "beta")
        result = self.server_module.handle_graphql(
            {"query": tampered, "variables": {"tickers": ["AAPL"]}}
        )
        self.assertIn("errors", result)
        self.assertIn("approved GetMarketData template", result["errors"][0]["message"])

    def test_missing_tickers_is_rejected(self):
        result = self.server_module.handle_graphql({"query": GRAPHQL_QUERY, "variables": {}})
        self.assertIn("errors", result)
        self.assertIn("$tickers", result["errors"][0]["message"])

    def test_live_server_round_trip(self):
        try:
            server = self.server_module.create_server(port=0)
        except OSError:
            self.skipTest("cannot bind a local socket in this environment")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/graphql"
            with mock.patch.dict(os.environ, {"MARKET_GRAPHQL_URL": url}):
                data = json.loads(fetch_market_data("AAPL,MSFT"))
            self.assertEqual(data["AAPL"]["price"], 175.20)
            self.assertEqual(data["MSFT"]["price"], 420.50)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
