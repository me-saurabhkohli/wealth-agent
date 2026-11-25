"""Market data through one approved GraphQL query template.

The model may change only the $tickers variable. The query text below is
fixed, which is the whole point of the article's GraphQL section: with a
fixed template the agent stops inventing fields and stops overfetching.

Fallback chain, in order:
1. A configured GraphQL endpoint (environment variable MARKET_GRAPHQL_URL).
   The exact query is sent as an HTTP POST with only $tickers varying.
2. Yahoo Finance through yfinance, when that package is installed.
3. Built-in mock data, so the demo runs with no network and no keys.

Every path returns the same shape, and the shape is checked against the
contract from the article before anything downstream sees it:

    type MarketData {
      ticker: String!
      price: Float!
      volatility30d: Float!
      sentiment: Float!
    }

The article's snippet used the gql library. This adapter sends the identical
query string with the standard library instead, to keep dependencies at
pyyaml only. A semaphore limits parallel calls per step, the fix the
article describes for the rate limiting failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.request

MARKET_DATA_SKILL = """
SKILL: Fetch market data using this EXACT query.
DO NOT modify the query structure. Only change the $tickers variable.
query GetMarketData($tickers: [String!]!) {
  marketData(tickers: $tickers) {
    ticker
    price
    volatility30d
    sentiment
  }
}
"""

GRAPHQL_QUERY = """query GetMarketData($tickers: [String!]!) {
  marketData(tickers: $tickers) {
    ticker
    price
    volatility30d
    sentiment
  }
}"""

# The article caps parallel endpoint calls per step with a semaphore of 5.
_REQUEST_SEMAPHORE = threading.Semaphore(5)

CONTRACT_FIELDS = ("price", "volatility", "sentiment")

# Mock prices. AAPL and MSFT use the example values printed in the article.
MOCK_MARKET_DATA = {
    "AAPL": {"price": 175.20, "volatility": 0.18, "sentiment": 0.7},
    "MSFT": {"price": 420.50, "volatility": 0.15, "sentiment": 0.8},
    "BND": {"price": 72.40, "volatility": 0.05, "sentiment": 0.5},
    "VTI": {"price": 268.10, "volatility": 0.13, "sentiment": 0.6},
}


class MarketDataError(RuntimeError):
    pass


def _validate_contract(data: dict) -> dict:
    """Fail loudly if any row is missing a contract field. A bad shape
    becomes an error that names the field, not a confident wrong answer."""
    for ticker, row in data.items():
        for field_name in CONTRACT_FIELDS:
            if field_name not in row or not isinstance(row[field_name], (int, float)):
                raise MarketDataError(
                    f"MarketData contract violated: {ticker} is missing numeric field '{field_name}'"
                )
    return data


def _mock_row(ticker: str) -> dict:
    if ticker in MOCK_MARKET_DATA:
        return dict(MOCK_MARKET_DATA[ticker])
    # Deterministic stand-in for tickers outside the built-in table, derived
    # from the ticker string so repeat runs agree with each other.
    digest = int(hashlib.sha256(ticker.encode()).hexdigest(), 16)
    return {
        "price": round(25 + (digest % 40000) / 100.0, 2),
        "volatility": round(0.05 + (digest % 30) / 100.0, 2),
        "sentiment": round(((digest >> 8) % 100) / 100.0, 2),
    }


def _fetch_from_endpoint(tickers: list[str], url: str) -> dict:
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"tickers": tickers}}).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with _REQUEST_SEMAPHORE, urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode())
    if "errors" in body:
        raise MarketDataError(f"GraphQL error from endpoint: {body['errors']}")
    rows = body["data"]["marketData"]
    data = {}
    for row in rows:
        data[row["ticker"]] = {
            "price": float(row["price"]),
            "volatility": float(row["volatility30d"]),
            "sentiment": float(row["sentiment"]),
        }
    missing = [t for t in tickers if t not in data]
    if missing:
        raise MarketDataError(f"Endpoint returned no rows for: {', '.join(missing)}")
    return data


def _fetch_from_yahoo(tickers: list[str]) -> dict:
    import yfinance  # optional dependency, imported only on this path

    data = {}
    for ticker in tickers:
        history = yfinance.Ticker(ticker).history(period="1mo")
        if history.empty:
            raise MarketDataError(f"Yahoo Finance returned no history for {ticker}")
        closes = history["Close"]
        price = float(closes.iloc[-1])
        returns = closes.pct_change().dropna()
        volatility = float(returns.std() * (252 ** 0.5)) if len(returns) > 1 else 0.0
        # Yahoo has no sentiment field. The contract requires one, so this
        # path reports a neutral 0.5 rather than inventing a signal.
        data[ticker] = {
            "price": round(price, 2),
            "volatility": round(volatility, 2),
            "sentiment": 0.5,
        }
    return data


def fetch_market_data(tickers: str) -> str:
    """Use the approved GraphQL skill to fetch prices, volatility, and sentiment.

    Accepts a comma separated string of tickers, exactly as the article's
    tool does, and returns a JSON string keyed by ticker.
    """
    wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not wanted:
        raise MarketDataError("No tickers supplied")

    url = os.environ.get("MARKET_GRAPHQL_URL")
    if url:
        return json.dumps(_validate_contract(_fetch_from_endpoint(wanted, url)))

    try:
        import yfinance  # noqa: F401

        return json.dumps(_validate_contract(_fetch_from_yahoo(wanted)))
    except ImportError:
        pass

    return json.dumps(_validate_contract({t: _mock_row(t) for t in wanted}))
