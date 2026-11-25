"""Worker prompts and model bindings.

The prompts for the market worker and the rebalancer are the ones printed in
the article. The supervisor and tax prompts follow the roles the article
describes: the supervisor plans and synthesizes, and the model in the tax
step only formats input. It never calculates.

Bindings say which provider each agent uses and which tools it may call.
The tax and compliance workers list no model tools that compute anything:
their answers come from the deterministic adapters.
"""

SUPERVISOR_PROMPT = """
You are the supervisor of a portfolio rebalancing workflow. Break the goal
into tasks, delegate market and tax work to the workers, wait for their
results, and synthesize. You cannot reorder the declared stages, skip one,
or call a worker the blueprint does not declare.
"""

MARKET_AGENT_PROMPT = """
You are a Market Data Specialist. Use fetch_market_data to get prices, 30d volatility, and sentiment.
Return a JSON object exactly like this:
{
  "AAPL": {"price": 175.20, "volatility": 0.18, "sentiment": 0.7},
  "MSFT": {"price": 420.50, "volatility": 0.15, "sentiment": 0.8}
}
Round to 2 decimals. No extra text.
"""

TAX_FORMATTER_PROMPT = """
You are a Tax Input Formatter. Format the holdings, tax lots, prices, and
target allocation as input for the tax optimizer and return its output as
JSON. Do not calculate tax, gains, or losses yourself.
"""

REBALANCER_AGENT_PROMPT = """
You are a Portfolio Rebalancing Specialist. Synthesize the outputs into a clear plan.
Inputs: tax_optimization_result, market_data, compliance_status.
Output: plain-English summary with current snapshot, recommended trades, tax impact, compliance note. Keep it concise.
"""

# Agent name -> binding. Provider names refer to blueprint.yaml.
AGENT_BINDINGS = {
    "supervisor": {
        "provider": "primary",
        "prompt": SUPERVISOR_PROMPT,
        "tools": [],
    },
    "market": {
        "provider": "primary",
        "prompt": MARKET_AGENT_PROMPT,
        "tools": ["market.fetch"],
    },
    "tax": {
        "provider": "primary",
        "prompt": TAX_FORMATTER_PROMPT,
        "tools": ["tax.optimize"],
    },
    "compliance": {
        "provider": "primary",
        "prompt": None,
        "tools": ["compliance.check"],
    },
    "rebalancer": {
        "provider": "small",
        "prompt": REBALANCER_AGENT_PROMPT,
        "tools": [],
    },
}
