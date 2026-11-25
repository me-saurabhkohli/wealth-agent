# Wealth Agent: the code behind "I Built an AI Wealth Manager"

This is the experiment code for the Medium article "I Built an AI Wealth
Manager. Then I Took Away Everything It Was Bad At." A supervisor agent
coordinates four workers to rebalance a portfolio for tax efficiency. The
model explains the plan. Code produces every number in it.

Disclaimer: this is a weekend experiment, not financial advice. Do not
deploy it with real money.

## Run the demo

No API keys are needed. The only required dependency is pyyaml.

```bash
pip install -r requirements.txt
python pipeline.py
```

The demo runs the article's example request (client ABC123, AAPL and MSFT
toward a 60/40 target, plus a bond holding), prints the market data, the
proposed trades, the compliance result, and the final plan, and writes a
trace to `runs/rebalance.jsonl`. To read a finished run back, step by step:

```bash
python pipeline.py --replay runs/rebalance.jsonl
```

Six synthetic clients live in `data/` and run the same way:

```bash
python pipeline.py --client SYN003
```

Tests: `python -m unittest discover -s tests -v`

## What the run looks like

```text
Rebalance for client ABC123: AAPL, MSFT, BND toward a 60/40 stock/bond target.
No API key set, so a deterministic mock model writes the plan text. The numbers are all computed by code.

  1. run       rebalance  client ABC123
  2. plan      supervisor [44 tokens]  Plan for client ABC123: fetch market data for AAPL, MSFT, BND; run tax optimization toward the target allocation; gate the proposed trades on compliance; then synthesize the plan.
  3. worker    market [57 tokens]  3 tickers via the approved query template
  4. adapter   market.fetch  fallback chain: endpoint, Yahoo, mock
  5. worker    tax  2 trades proposed
  6. adapter   tax.optimize  deterministic Oracle math, no model call
  7. gate      compliance.check  status: pass
  8. synthesis rebalancer [67 tokens]  plan written; human checkpoint before execution
  9. run       rebalance  complete, 168 tokens used

Final plan
Rebalance plan: Sell 68.17 shares of AAPL at $175.20 (about $11,942.81). Buy 164.96 shares of BND at $72.40 (about $11,942.81). The sales harvest a net loss of $1,008.87, an estimated tax saving of $252.22. Allocation drift falls by 53.87 points. Compliance status: pass.
```

The gate is real, not decoration. Give the demo a portfolio that would
break a rule (one stock headed for 60 percent of the portfolio, over the
50 percent limit) and the run stops at step 7: status blocked, no plan,
and the reason printed. The full result is also available as JSON with
`python pipeline.py --json`.

With no key set, a deterministic mock model writes the supervisor plan and
the rebalancer paragraph from the real computed numbers. If
`OPENAI_API_KEY` is set (see `.env.example`), the rebalancer paragraph
comes from the model named in the blueprint instead. Either way, the tax
math and the compliance checks never use a model.

## How the pieces map to the article

| Article section | Code |
| --- | --- |
| One Blueprint, Four Workers, No Shared Notes | `blueprint.yaml`, validated by `runtime.py` before anything runs |
| First, I Stopped the Agent From Writing Its Own Queries | `adapters/graphql_client.py`: one approved query template, only `$tickers` varies |
| The Step the Supervisor Cannot Skip | Stage order enforced in `runtime.py`: plan, market, tax, compliance gate, synthesis |
| Market Agent | `agents.py` prompt plus the regex JSON extraction and contract check in `runtime.py` |
| Tax Agent | `adapters/tax.py` over `adapters/oracle.py`, deterministic tax-lot math with wash sale awareness and short/long classification |
| Compliance Agent | `adapters/compliance.py`: a small rules engine, five rules declared as data, pass or fail with reasons |
| Rebalancer Agent | `agents.py` prompt; writes the readable plan from the workers' JSON |
| The Flight Recorder, Declared This Time | `trace.py`: event bus, JSONL export, cost tracking, replay |

## The blueprint

`blueprint.yaml` declares the providers (gpt-5.6-sol for the primary
workers, gpt-5.6-luna for the rebalancer), the agents, the supervisor
workflow, the output contracts (MarketData, TaxResult, ComplianceResult),
a token budget of 60000 per run, a human checkpoint before execution, and
tracing. The runtime checks every reference before a single model call:
a workflow that names a missing agent, or a contract that names an
unknown worker, fails validation instead of failing live.

## Market data fallback chain

`fetch_market_data` tries, in order:

1. A GraphQL endpoint from the `MARKET_GRAPHQL_URL` environment variable,
   sent the exact approved query with only `$tickers` varying.
2. Yahoo Finance, if the optional `yfinance` package is installed. Yahoo
   has no sentiment field, so that path reports a neutral 0.5 rather than
   inventing one.
3. Built-in mock data. AAPL and MSFT use the example values printed in the
   article.

Every path is checked against the MarketData contract (price,
volatility30d, sentiment) before downstream code sees it.

## Synthetic clients

`data/` holds six portfolios as JSON request files, in the same shape as
the article's demo request. They are generated by
`data/generate_synthetic.py` (fixed seed, so regeneration is byte for
byte identical) and each one exists to exercise a specific path:

| Client | What it demonstrates |
| --- | --- |
| SYN001 | A clean harvest: stock heavy, one deep loss lot, every rule passes |
| SYN002 | A loss sale inside the wash window in recent sales; the plan sells more of that ticker and buys bonds, so nothing conflicts |
| SYN003 | The wash sale skip: a recently acquired AAPL lot stays in the portfolio, so the optimizer skips the deep AAPL loss lot and sells gains instead; the skipped lot is reported in the tax result |
| SYN004 | A bond heavy portfolio harvesting an AGG loss, with short term lots in the mix |
| SYN005 | A newer portfolio with a recent lot at a gain; the harvest comes from a different ticker and the run passes |
| DEMO_BLOCKED | One stock and almost nothing else: the target is reachable but MSFT would still be 60 percent of the portfolio, over the concentration limit, so the gate blocks the run |

The generator checks its own work as it writes: every portfolio runs
through the real tax and compliance adapters, and generation fails if a
client designed to pass gets blocked, or the blocked client passes.

## Contracts

The shapes the blueprint names are real files in `contracts/`:

- `schema.graphql`: the GraphQL schema from the article, the contract an
  endpoint has to honor.
- `approved_query.graphql`: the one query the market worker may send.
- `market_data.schema.json`, `tax_result.schema.json`,
  `compliance_result.schema.json`: JSON Schemas for the three worker
  outputs.

The runtime validates every worker output against these schemas before
the next stage sees it, on top of the field check the article describes.
The validator is hand rolled for the subset of JSON Schema the files use
(`contracts/validate.py`), so the core still needs only pyyaml. A
violation raises an error naming the contract and the offending path,
and the tests cover malformed payloads for all three contracts.

A mock endpoint that enforces the query template lives at
`tools/mock_graphql_server.py` (standard library only):

```bash
python tools/mock_graphql_server.py 8765
MARKET_GRAPHQL_URL=http://127.0.0.1:8765/graphql python pipeline.py
```

It accepts only the approved GetMarketData query (whitespace
normalized); any other query, an invented field included, gets a GraphQL
error naming the problem. Matching queries return the same mock rows the
offline path serves, so a run against the endpoint produces the same
numbers as a run with no endpoint at all.

## The tax optimizer and the rules engine

The Oracle optimizer (`adapters/oracle.py`) sells specific lots: losses
first, largest loss per share first, then the smallest gains, until the
overweight class reaches the target. Two refinements matter:

- Wash sale awareness. If a plan would leave recently acquired shares
  of a ticker in the portfolio while selling other lots of it at a loss,
  the retained shares are replacement shares and the loss would be
  disallowed, so the optimizer runs its walk a second time with those
  loss lots skipped, and reports them in `skipped_lots` with the reason.
  A full exit leaves no replacement shares and is never skipped.
- Short and long term. Each sale is classified from its lot (held more
  than one year is long term) and reported per trade, with the sums in
  `gain_loss_breakdown`. The headline savings estimate keeps the single
  documented assumption, an assumed 25 percent marginal rate on the net
  harvested loss; the breakdown is reporting, not a second estimate.

Compliance (`adapters/compliance.py`) is a small rules engine in the
shape of the article's snippet: rules are declared as data, a
RuleRowDecision binds one rule to its check, and a RuleSetDecision
evaluates the set into the contract result. The five rules:

| Rule | What it checks |
| --- | --- |
| concentration_limit | Largest post-trade position at most 50 percent of the portfolio |
| wash_sale_window | No planned buy of a ticker sold at a loss within 30 days, in recent sales or in the plan itself |
| drift_tolerance | Post-trade drift from the target at most 5 points |
| sell_within_holdings | No planned sale exceeds the shares held |
| cash_residual | Buys are funded by sale proceeds, and unspent proceeds stay under 5 percent of the portfolio |

## The demo UI

`docs/` is a static page (no build step, no external assets) that plays
back recorded runs of every client: pick a client, read the portfolio
and its tax lots, step or play through the trace, and see the proposed
trades, the compliance checklist, and the final plan or the block
reasons. It is labeled as what it is: recordings of the Python demo on
synthetic data.

Regenerate the recordings after changing the demo:

```bash
python tools/build_demo_data.py
```

That writes `docs/data.json` and the same payload as `docs/data.js`
(the script copy lets the page work opened as a plain file, where
fetching a local JSON file is unreliable). To view locally, open
`docs/index.html` in a browser, or serve the folder with
`python -m http.server --directory docs`.

To publish on GitHub Pages, one setting in the repository: Settings,
Pages, then under "Build and deployment" choose "Deploy from a branch",
branch `main`, folder `/docs`. Every path in the page is relative, so it
works under the project subpath GitHub Pages assigns.

## Honest notes on where code and article differ

- The article's Oracle library is a private package, so
  `adapters/oracle.py` is a self-contained implementation with the same
  job and output shape: tax-lot aware trades (losses harvested first), a
  tax savings estimate, and drift reduction. Its savings estimate values a
  harvested loss at an assumed 25 percent marginal rate, stated in the
  code. The method is simple on purpose, so every number can be checked
  by hand. Two simplifications to know about: when a loss lot falls
  inside the wash sale window the optimizer skips the whole lot rather
  than splitting the disallowance share by share, and the short/long
  classification uses a 365 day line rather than the exact
  more-than-one-year rule with its edge cases.
- The article's compliance snippet used a rules engine package
  (simple-rule-engine, with RuleSetDecision and RuleRowDecision types).
  The engine here is a small local implementation with the same class
  names and the same behavior (fixed rules, reproducible pass or fail),
  extended from the article's two checks to five rules. Swapping in the
  real package later should be a wiring change, not a redesign.
- The article's pipeline snippet imported `load_blueprint` and
  `compile_blueprint` as if from a framework, and `TraceEventBus`,
  `JsonlExporter`, and `CostTracker` from `pyagent_trace`. The real
  packages exist (pyagent-blueprint and pyagent-trace on PyPI, from
  github.com/pyagent-core/pyagent, and pyagent-trace really does export
  those trace names), but the exact function level API in the snippets
  could not be verified, so `runtime.py` and `trace.py` are small local
  implementations with the same names and semantics. Swapping in the real
  packages later should be a wiring change, not a redesign.
- The article's tax snippet took holdings alone and hard-coded the 60/40
  target. `run_tax_optimization` takes holdings, prices, and targets in
  one JSON payload, which keeps it a pure function: same input, same
  output, every run.
- In the article the supervisor delegates market and tax work in parallel
  where the workflow allows. Here the tax worker consumes market prices,
  so market lands first. Neither worker sees the other's intermediate
  steps, only its returned answer.
- The demo never executes trades. The blueprint's human checkpoint sits
  between the plan and any execution, and there is deliberately no
  execution code in this project.
