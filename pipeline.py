"""Loads the blueprint, binds adapters, runs a request.

Kept thin on purpose, the way the article describes: the blueprint carries
the structure, and this file wires it together.

Usage:
    python pipeline.py                 Run the demo rebalance (no keys needed)
    python pipeline.py --client SYN001 Run a synthetic client from data/
    python pipeline.py --replay runs/rebalance.jsonl
                                       Print a recorded run, step by step

The demo request is the article's example: client ABC123, AAPL and MSFT
with a 60/40 target, plus a bond holding so the rebalance has somewhere
to go. With no OPENAI_API_KEY set, a deterministic mock model produces
the supervisor plan and the rebalancer paragraph from the real computed
numbers. The GraphQL template, the tax math, and the compliance rules
genuinely execute on every run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from adapters.compliance import run_compliance_check
from adapters.graphql_client import fetch_market_data
from adapters.tax import run_tax_optimization
from runtime import LiveModel, compile_blueprint, load_blueprint
from trace import CostTracker, JsonlExporter, TraceEventBus, format_replay, replay

ADAPTERS = {
    "market.fetch": fetch_market_data,         # GraphQL template
    "tax.optimize": run_tax_optimization,      # Oracle
    "compliance.check": run_compliance_check,  # rules engine
}

DEMO_REQUEST = {
    "client_id": "ABC123",
    "goal": "Rebalance client ABC123 for tax efficiency, target 60/40",
    "targets": {"stock": 0.6, "bond": 0.4},
    "as_of": "2026-11-24",
    "holdings": [
        {
            "ticker": "AAPL",
            "asset_class": "stock",
            "shares": 100,
            "lots": [
                {"shares": 30, "cost_basis": 120.00, "acquired": "2023-03-01"},
                {"shares": 70, "cost_basis": 190.00, "acquired": "2025-09-15"},
            ],
        },
        {
            "ticker": "MSFT",
            "asset_class": "stock",
            "shares": 50,
            "lots": [
                {"shares": 50, "cost_basis": 300.00, "acquired": "2022-06-01"},
            ],
        },
        {
            "ticker": "BND",
            "asset_class": "bond",
            "shares": 80,
            "lots": [
                {"shares": 80, "cost_basis": 70.00, "acquired": "2024-01-15"},
            ],
        },
    ],
    "recent_sales": [],
}


TRACE_PATH = "runs/rebalance.jsonl"


def build_runtime(trace_path: str = TRACE_PATH):
    blueprint = load_blueprint(os.path.join(os.path.dirname(__file__), "blueprint.yaml"))
    bus = TraceEventBus()
    bus.subscribe(JsonlExporter(trace_path))
    cost_tracker = CostTracker()
    bus.subscribe(cost_tracker)

    model = None
    if os.environ.get("OPENAI_API_KEY"):
        model = LiveModel(blueprint["providers"]["small"]["model"])

    runtime = compile_blueprint(blueprint, adapters=ADAPTERS, trace_bus=bus, model=model)
    return runtime, cost_tracker


def main() -> int:
    parser = argparse.ArgumentParser(description="Wealth manager rebalance demo")
    parser.add_argument("--replay", metavar="PATH", help="Replay a recorded run from its JSONL trace")
    parser.add_argument("--request", metavar="PATH", help="Run a rebalance request from a JSON file instead of the demo")
    parser.add_argument("--client", metavar="CLIENT_ID", help="Run a synthetic client from data/<CLIENT_ID>.json (ABC123 runs the article demo)")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON instead of the step by step view")
    args = parser.parse_args()

    if args.replay:
        print(format_replay(replay(args.replay)))
        return 0

    request = DEMO_REQUEST
    trace_path = TRACE_PATH
    if args.client:
        if args.client == DEMO_REQUEST["client_id"]:
            request = DEMO_REQUEST
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{args.client}.json")
            if not os.path.exists(path):
                print(f"No synthetic client file at {path}. Run data/generate_synthetic.py first.")
                return 2
            with open(path) as fh:
                request = json.load(fh)
            trace_path = f"runs/{args.client}.jsonl"
    elif args.request:
        with open(args.request) as fh:
            request = json.load(fh)

    os.makedirs("runs", exist_ok=True)
    runtime, cost_tracker = build_runtime(trace_path=trace_path)
    result = runtime.run("rebalance", request)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "complete" else 1

    tickers = ", ".join(h["ticker"] for h in request["holdings"])
    stock_pct = round(request["targets"]["stock"] * 100)
    bond_pct = round(request["targets"]["bond"] * 100)
    print(f"Rebalance for client {request['client_id']}: {tickers} toward a "
          f"{stock_pct}/{bond_pct} stock/bond target.")
    if os.environ.get("OPENAI_API_KEY"):
        print("A live model writes the plan text. The numbers are all computed by code.")
    else:
        print("No API key set, so a deterministic mock model writes the plan text. "
              "The numbers are all computed by code.")
    print()
    print(format_replay(replay(trace_path)))
    print()
    if result["status"] == "complete":
        print("Final plan")
        print(result["plan"])
    else:
        print("No plan produced: the compliance gate blocked the proposed trades.")
        for reason in result.get("compliance", {}).get("reasons", []):
            print(f"- {reason}")
    print()
    print(f"Total tokens: {cost_tracker.total_tokens} (the deterministic steps record 0)")
    print(f"Trace written to {trace_path}. Read it back any time with:")
    print(f"    python pipeline.py --replay {trace_path}")
    print("Reminder: the human checkpoint sits before execution. This demo never executes trades.")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
