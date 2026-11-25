"""A mock GraphQL endpoint that enforces the approved query.

Standard library only. The server accepts POSTed GraphQL requests and
compares the query text, whitespace normalized, against the approved
GetMarketData template from contracts/approved_query.graphql (the same
text adapters/graphql_client.py sends). Any other query gets a GraphQL
error naming the problem, which is the behavior the article describes: a
bad request comes back as an error that names the field, instead of a
confident answer built on a field the model had guessed. Matching
queries get contract-valid mock data for the requested tickers.

Run it:

    python tools/mock_graphql_server.py [port]        # default 8765

Then point the demo at it:

    MARKET_GRAPHQL_URL=http://127.0.0.1:8765/graphql python pipeline.py

With the endpoint set, the market worker's data really travels over HTTP
through the approved template, and the run produces the same numbers as
the offline mock path, because both serve the same mock rows.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from adapters.graphql_client import GRAPHQL_QUERY, _mock_row

TEMPLATE_PROBLEM = (
    "Query does not match the approved GetMarketData template. "
    "Only the $tickers variable may change; the query structure is fixed "
    "by contracts/approved_query.graphql."
)


def normalize_query(text: str) -> str:
    """Collapse every run of whitespace to one space so formatting alone
    never decides whether a query matches the template."""
    return " ".join(text.split())


APPROVED_NORMALIZED = normalize_query(GRAPHQL_QUERY)


def handle_graphql(body: dict) -> dict:
    """Pure request handler, kept separate from the HTTP plumbing so the
    tests can exercise it without a socket."""
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"errors": [{"message": "Request carries no GraphQL query text."}]}
    if normalize_query(query) != APPROVED_NORMALIZED:
        return {"errors": [{"message": TEMPLATE_PROBLEM}]}
    variables = body.get("variables") or {}
    tickers = variables.get("tickers")
    if not isinstance(tickers, list) or not tickers or not all(isinstance(t, str) for t in tickers):
        return {
            "errors": [
                {"message": "Variable $tickers must be a non-empty list of ticker strings."}
            ]
        }
    rows = []
    for ticker in tickers:
        row = _mock_row(ticker.strip().upper())
        rows.append(
            {
                "ticker": ticker.strip().upper(),
                "price": row["price"],
                "volatility30d": row["volatility"],
                "sentiment": row["sentiment"],
            }
        )
    return {"data": {"marketData": rows}}


class GraphQLHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            body = {}
            result = {"errors": [{"message": "Request body is not valid JSON."}]}
        else:
            result = handle_graphql(body)
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # keep the demo output clean
        pass


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), GraphQLHandler)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = create_server(port=port)
    print(f"Mock GraphQL endpoint listening on http://127.0.0.1:{port}/graphql")
    print("It accepts only the approved GetMarketData query; anything else gets an error.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
