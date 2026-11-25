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

Tests: `python -m unittest discover -s tests -v`

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
| Tax Agent | `adapters/tax.py` over `adapters/oracle.py`, deterministic tax-lot math |
| Compliance Agent | `adapters/compliance.py`: concentration and wash sale rules, pass or fail with reasons |
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

## Honest notes on where code and article differ

- The article's Oracle library is a private package, so
  `adapters/oracle.py` is a self-contained implementation with the same
  job and output shape: tax-lot aware trades (losses harvested first), a
  tax savings estimate, and drift reduction. Its savings estimate values a
  harvested loss at an assumed 25 percent marginal rate, stated in the
  code. The method is simple on purpose, so every number can be checked
  by hand.
- The article's compliance snippet used a rules engine package. Here the
  two rules (a 50 percent single position limit, and the 30 day wash sale
  window checked against a precomputed conflict list) are plain Python
  functions. Same behavior: fixed rules, reproducible pass or fail.
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
