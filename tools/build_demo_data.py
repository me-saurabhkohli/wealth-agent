"""Build docs/data.json (and docs/data.js) for the GitHub Pages UI.

Runs the real pipeline over the article's client (ABC123) and every
synthetic client in data/, and records each run in full: the request,
the market data, the tax result, the compliance checks, the final plan
or the block reasons, the token usage, and the trace events. The UI in
docs/ replays these recordings; it never runs Python in the browser.

Deterministic by construction: the tool forces the offline path (no
OPENAI_API_KEY, no MARKET_GRAPHQL_URL, no Yahoo fallback), and trace
timestamps are dropped, because a recording that changes every time it
is regenerated is not a recording. Run it after changing the demo and
commit the regenerated files with the change.

Usage: python tools/build_demo_data.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Force the deterministic offline path before importing the pipeline.
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("MARKET_GRAPHQL_URL", None)
sys.modules["yfinance"] = None  # makes the optional Yahoo import fail fast

from pipeline import DEMO_REQUEST, build_runtime

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DOCS_DIR = os.path.join(ROOT, "docs")


def load_clients() -> list[dict]:
    clients = [DEMO_REQUEST]
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "*.json"))):
        with open(path) as fh:
            clients.append(json.load(fh))
    return clients


def record_run(request: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        trace_path = os.path.join(tmp, "trace.jsonl")
        runtime, tracker = build_runtime(trace_path=trace_path)
        result = runtime.run("rebalance", request)
        events = [
            {key: event[key] for key in ("seq", "kind", "name", "detail", "tokens")}
            for event in (e.to_dict() for e in runtime.bus.events)
        ]
    return {
        "client_id": request["client_id"],
        "goal": request["goal"],
        "targets": request["targets"],
        "as_of": request["as_of"],
        "holdings": request["holdings"],
        "recent_sales": request.get("recent_sales", []),
        "status": result["status"],
        "market_data": result["market_data"],
        "tax_result": result["tax_result"],
        "compliance": result["compliance"],
        "plan": result["plan"],
        "block_reasons": result["compliance"]["reasons"] if result["status"] != "complete" else [],
        "tokens_used": tracker.total_tokens,
        "tokens_by_step": tracker.by_name,
        "trace": events,
    }


def main() -> int:
    records = [record_run(client) for client in load_clients()]
    payload = {
        "about": (
            "Recorded runs of the wealth-agent Python demo on synthetic data. "
            "Nothing here is a live system or financial advice."
        ),
        "clients": records,
    }
    os.makedirs(DOCS_DIR, exist_ok=True)
    json_path = os.path.join(DOCS_DIR, "data.json")
    with open(json_path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    # The same payload as a script, so the page also works opened as a
    # plain file, where fetch() of a local JSON file is not reliable.
    js_path = os.path.join(DOCS_DIR, "data.js")
    with open(js_path, "w") as fh:
        fh.write("window.DEMO_DATA = ")
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write(";\n")
    for record in records:
        print(f"recorded {record['client_id']}: {record['status']}, {record['tokens_used']} tokens")
    print(f"wrote {json_path} and {js_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
