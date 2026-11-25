"""Blueprint loading, validation, and the supervisor runtime.

The article writes:

    blueprint = load_blueprint("blueprint.yaml")
    runtime = compile_blueprint(blueprint, adapters={...})
    result = runtime.run("rebalance", request)

The real pyagent-blueprint package exists on PyPI (version 0.3.1, from
github.com/pyagent-core/pyagent) and validates and compiles blueprint
specs, but its exact function level API could not be verified against the
article's trimmed snippets from here. So this project ships a small local
loader and compiler that provide the article's semantics for this
blueprint:

- load_blueprint parses the YAML and checks api_version.
- compile_blueprint validates before anything runs: every workflow agent
  exists, every provider reference resolves, every contract names a known
  worker, and every adapter the workers need is bound. A dangling
  reference fails here, not in a live run.
- Runtime.run executes the supervisor pattern with the stage order fixed
  by the blueprint: plan, then market and tax in parallel, then the
  compliance gate, then synthesis. Synthesis cannot happen before the
  gate. That ordering is the article's central claim, so it lives in code,
  not in a prompt.

Models: with no API key the runtime uses MockModel, a deterministic stand
in that produces the supervisor plan and the rebalancer paragraph from the
real computed numbers. If OPENAI_API_KEY is set, a model call is attempted
for the rebalancer text only; tax and compliance never use a model either
way. Token counts are estimated from text length for the mock, and the
run's token budget from the blueprint is enforced.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

import yaml

from agents import AGENT_BINDINGS
from contracts import SchemaValidationError
from contracts import validate as validate_contract_schema
from trace import TraceEventBus

REQUIRED_CONTRACT_FIELDS = {
    "MarketData": ("price", "volatility", "sentiment"),
    "TaxResult": ("recommended_trades", "tax_savings_estimate", "drift_reduction"),
    "ComplianceResult": ("status", "checks", "reasons"),
}


class BlueprintError(RuntimeError):
    pass


class ContractError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def load_blueprint(path: str) -> dict:
    with open(path) as fh:
        blueprint = yaml.safe_load(fh)
    if not isinstance(blueprint, dict):
        raise BlueprintError(f"{path} did not parse to a mapping")
    if blueprint.get("api_version") != "pyagent/v1":
        raise BlueprintError(f"Unsupported api_version: {blueprint.get('api_version')!r}")
    return blueprint


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockModel:
    """Deterministic model stand in. Produces text from computed inputs,
    so the demo exercises the real orchestration with no API key."""

    name = "mock"

    def plan(self, request: dict) -> str:
        tickers = ", ".join(h["ticker"] for h in request["holdings"])
        text = (
            f"Plan for client {request.get('client_id', 'unknown')}: fetch market data for "
            f"{tickers}; run tax optimization toward the target allocation; "
            f"gate the proposed trades on compliance; then synthesize the plan."
        )
        return text

    def market_reply(self, market_data: dict) -> str:
        # The article describes the model opening some replies with a
        # greeting despite a prompt that ends with "No extra text". The
        # mock reproduces that habit so the regex fallback below is real
        # code on a real path, not decoration.
        return "Here is the market data you requested.\n" + json.dumps(market_data)

    def synthesize(self, tax_result: dict, market_data: dict, compliance_result: dict) -> str:
        lines = []
        for trade in tax_result["recommended_trades"]:
            verb = "Sell" if trade["side"] == "sell" else "Buy"
            lines.append(
                f"{verb} {trade['shares']:.2f} shares of {trade['ticker']} at "
                f"${trade['price']:.2f} (about ${trade['proceeds']:,.2f})."
            )
        if tax_result["net_realized_gain_loss"] < 0:
            tax_note = (
                f"The sales harvest a net loss of ${abs(tax_result['net_realized_gain_loss']):,.2f}, "
                f"an estimated tax saving of ${tax_result['tax_savings_estimate']:,.2f}."
            )
        else:
            tax_note = (
                f"The sales realize a net gain of ${tax_result['net_realized_gain_loss']:,.2f}; "
                f"no loss was available to harvest in this run."
            )
        plan = (
            "Rebalance plan: " + " ".join(lines) + " " + tax_note
            + f" Allocation drift falls by {tax_result['drift_reduction']:.2f} points. "
            + f"Compliance status: {compliance_result['status']}."
        )
        return plan


class LiveModel(MockModel):
    """Uses the OpenAI chat API for the rebalancer paragraph only, when a
    key is configured. Planning and market replies stay mock so the demo
    path and the live path differ in exactly one place."""

    name = "openai"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def synthesize(self, tax_result: dict, market_data: dict, compliance_result: dict) -> str:
        from agents import REBALANCER_AGENT_PROMPT

        payload = {
            "model": self.model_id.replace("openai:", ""),
            "messages": [
                {"role": "system", "content": REBALANCER_AGENT_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tax_optimization_result": tax_result,
                            "market_data": market_data,
                            "compliance_status": compliance_result,
                        }
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode())
        return body["choices"][0]["message"]["content"]


def extract_json_object(text: str) -> dict:
    """Regex fallback from the article: pull the first JSON object out of a
    model reply that may carry prose around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ContractError("Model reply contained no JSON object")
    return json.loads(match.group(0))


def check_contract(contract_name: str, payload) -> None:
    """Two layers, both from the contracts the blueprint names. The field
    check below is the fast shape test the article describes; the JSON
    Schema in contracts/ then validates the full payload, types and
    nested trade rows included. A violation raises ContractError naming
    the contract and the offending path."""
    try:
        validate_contract_schema(contract_name, payload)
    except SchemaValidationError as exc:
        raise ContractError(str(exc)) from exc
    fields = REQUIRED_CONTRACT_FIELDS[contract_name]
    if isinstance(payload, dict) and payload and all(isinstance(v, dict) for v in payload.values()):
        for key, row in payload.items():
            for field_name in fields:
                if field_name not in row:
                    raise ContractError(f"{contract_name} contract violated: {key} missing '{field_name}'")
        return
    for field_name in fields:
        if field_name not in payload:
            raise ContractError(f"{contract_name} contract violated: missing '{field_name}'")


class Runtime:
    def __init__(self, blueprint: dict, adapters: dict, trace_bus: TraceEventBus | None = None, model=None) -> None:
        self.blueprint = blueprint
        # Copy the bindings so a caller (or a test) swapping one adapter
        # on a built runtime cannot leak the change into other runtimes.
        self.adapters = dict(adapters)
        self.bus = trace_bus or TraceEventBus()
        self.model = model or MockModel()
        self.budget = int(blueprint.get("governance", {}).get("token_budget_per_run", 0)) or None
        self.tokens_used = 0

    def _spend(self, tokens: int) -> None:
        self.tokens_used += tokens
        if self.budget and self.tokens_used > self.budget:
            raise BudgetExceeded(
                f"Token budget exceeded: {self.tokens_used} used of {self.budget} allowed per run"
            )

    def run(self, workflow_name: str, request: dict) -> dict:
        workflows = self.blueprint.get("workflows", {})
        if workflow_name not in workflows:
            raise BlueprintError(f"Unknown workflow: {workflow_name}")
        workflow = workflows[workflow_name]
        workers = workflow["agents"]["workers"]
        bus = self.bus
        bus.publish("run", workflow_name, f"client {request.get('client_id', 'unknown')}")

        # Stage 1: the supervisor plans. A model step, so it costs tokens.
        plan = self.model.plan(request)
        plan_tokens = _estimate_tokens(plan)
        self._spend(plan_tokens)
        bus.publish("plan", "supervisor", plan, tokens=plan_tokens)

        tickers = ",".join(h["ticker"] for h in request["holdings"])

        # Stage 2: market, then tax. The article has the supervisor
        # delegate market and tax work in parallel where the workflow
        # allows it. Here the tax worker consumes market prices, so the
        # market step lands first; neither worker sees the other's
        # intermediate steps, only its returned answer.
        raw = self.adapters["market.fetch"](tickers)
        reply = self.model.market_reply(json.loads(raw))
        market_tokens = _estimate_tokens(reply)
        self._spend(market_tokens)
        market_data = extract_json_object(reply)
        check_contract("MarketData", market_data)
        bus.publish("worker", "market", f"{len(market_data)} tickers via the approved query template", tokens=market_tokens)
        bus.publish("adapter", "market.fetch", "fallback chain: endpoint, Yahoo, mock", tokens=0)

        tax_payload = {
            "holdings": request["holdings"],
            "prices": {t: market_data[t]["price"] for t in market_data},
            "targets": request.get("targets") or {"stock": 0.6, "bond": 0.4},
            "recent_sales": request.get("recent_sales", []),
            "as_of": request.get("as_of", "2026-11-24"),
        }
        tax_result = json.loads(self.adapters["tax.optimize"](json.dumps(tax_payload)))
        check_contract("TaxResult", tax_result)
        bus.publish("worker", "tax", f"{len(tax_result['recommended_trades'])} trades proposed", tokens=0)
        bus.publish("adapter", "tax.optimize", "deterministic Oracle math, no model call", tokens=0)

        # Stage 3: the compliance gate. Declared in the blueprint, enforced
        # here. If it fails, there is no synthesis and no plan to execute.
        compliance_payload = {
            "trades": tax_result["recommended_trades"],
            "holdings": request["holdings"],
            "prices": {t: market_data[t]["price"] for t in market_data},
            "targets": request.get("targets") or {"stock": 0.6, "bond": 0.4},
            "recent_sales": request.get("recent_sales", []),
            "as_of": request.get("as_of", "2026-11-24"),
        }
        compliance_result = json.loads(self.adapters["compliance.check"](json.dumps(compliance_payload)))
        check_contract("ComplianceResult", compliance_result)
        bus.publish("gate", "compliance.check", f"status: {compliance_result['status']}", tokens=0)
        if compliance_result["status"] != "pass":
            bus.publish("run", workflow_name, "blocked at the compliance gate; no plan produced")
            return {
                "status": "blocked",
                "reason": "compliance gate failed",
                "compliance": compliance_result,
                "market_data": market_data,
                "tax_result": tax_result,
                "plan": None,
            }

        # Stage 4: synthesis, the rebalancer's readable plan. The human
        # checkpoint declared in the blueprint sits after this step,
        # before any execution. This runtime never executes trades.
        plan_text = self.model.synthesize(tax_result, market_data, compliance_result)
        synth_tokens = _estimate_tokens(plan_text)
        self._spend(synth_tokens)
        bus.publish("synthesis", "rebalancer", "plan written; human checkpoint before execution", tokens=synth_tokens)
        bus.publish("run", workflow_name, f"complete, {self.tokens_used} tokens used")

        return {
            "status": "complete",
            "market_data": market_data,
            "tax_result": tax_result,
            "compliance": compliance_result,
            "plan": plan_text,
            "tokens_used": self.tokens_used,
        }


def compile_blueprint(blueprint: dict, adapters: dict, trace_bus: TraceEventBus | None = None, model=None) -> Runtime:
    """Validate the blueprint against the bindings and adapters, then
    return a runnable Runtime. Validation happens before any model call."""
    providers = blueprint.get("providers", {})
    agents = blueprint.get("agents", {})
    workflows = blueprint.get("workflows", {})
    contracts = blueprint.get("contracts", {})

    for agent_name, spec in agents.items():
        provider = spec.get("provider")
        if provider not in providers:
            raise BlueprintError(f"Agent '{agent_name}' references unknown provider '{provider}'")
        if agent_name not in AGENT_BINDINGS:
            raise BlueprintError(f"Agent '{agent_name}' has no binding in agents.py")
        binding_provider = AGENT_BINDINGS[agent_name]["provider"]
        if binding_provider != provider:
            raise BlueprintError(
                f"Agent '{agent_name}' provider mismatch: blueprint says '{provider}', agents.py says '{binding_provider}'"
            )

    for workflow_name, workflow in workflows.items():
        spec = workflow.get("agents", {})
        lead = spec.get("lead")
        if lead and lead not in agents:
            raise BlueprintError(f"Workflow '{workflow_name}' lead '{lead}' is not a declared agent")
        for worker in spec.get("workers", []):
            if worker not in agents:
                raise BlueprintError(f"Workflow '{workflow_name}' references unknown worker '{worker}'")

    for worker, contract in contracts.items():
        if worker not in agents:
            raise BlueprintError(f"Contract names unknown worker '{worker}'")
        output = contract.get("output")
        if output not in REQUIRED_CONTRACT_FIELDS:
            raise BlueprintError(f"Contract for '{worker}' names unknown output type '{output}'")

    needed = {"market.fetch", "tax.optimize", "compliance.check"}
    missing = needed - set(adapters)
    if missing:
        raise BlueprintError(f"Adapters not bound: {', '.join(sorted(missing))}")

    return Runtime(blueprint, adapters, trace_bus=trace_bus, model=model)
