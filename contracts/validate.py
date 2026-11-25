"""Worker contracts as files, and a small validator for them.

The contracts directory holds the shapes the blueprint names:

- schema.graphql: the GraphQL schema from the article, the contract an
  endpoint has to honor.
- approved_query.graphql: the one query the market worker may send.
- market_data.schema.json, tax_result.schema.json,
  compliance_result.schema.json: JSON Schemas for the three worker
  outputs (MarketData, TaxResult, ComplianceResult).

validate() checks a payload against the JSON Schema for a contract name.
The validator is hand rolled for the subset of JSON Schema these files
use (types, required, properties, additionalProperties, items, enum,
minimum, maximum, minProperties), so the project keeps its single
dependency: pyyaml for the core, standard library for everything else.
"""

from __future__ import annotations

import json
import os

CONTRACT_SCHEMAS = {
    "MarketData": "market_data.schema.json",
    "TaxResult": "tax_result.schema.json",
    "ComplianceResult": "compliance_result.schema.json",
}

_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE: dict[str, dict] = {}


class SchemaValidationError(RuntimeError):
    """Raised when a payload does not match its contract schema. The
    message names the contract and the path of the offending value."""


def load_schema(contract_name: str) -> dict:
    if contract_name not in CONTRACT_SCHEMAS:
        raise SchemaValidationError(f"Unknown contract: {contract_name}")
    if contract_name not in _CACHE:
        path = os.path.join(_DIR, CONTRACT_SCHEMAS[contract_name])
        with open(path) as fh:
            _CACHE[contract_name] = json.load(fh)
    return _CACHE[contract_name]


def _type_matches(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError(f"Schema uses unsupported type: {expected}")


def _check(value, schema: dict, contract_name: str, path: str) -> None:
    def fail(message: str) -> None:
        raise SchemaValidationError(f"{contract_name} contract violated at {path}: {message}")

    if "enum" in schema and value not in schema["enum"]:
        fail(f"value {value!r} is not one of {schema['enum']!r}")

    expected_types = schema.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_type_matches(value, t) for t in expected_types):
            fail(f"expected type {' or '.join(expected_types)}, got {type(value).__name__}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"value {value} is below the minimum of {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"value {value} is above the maximum of {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                fail(f"missing required field '{key}'")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            fail(f"expected at least {schema['minProperties']} properties, got {len(value)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], contract_name, f"{path}.{key}")
            elif isinstance(additional, dict):
                _check(item, additional, contract_name, f"{path}.{key}")
            elif additional is False:
                fail(f"unexpected field '{key}'")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], contract_name, f"{path}[{index}]")


def validate(contract_name: str, payload) -> None:
    """Raise SchemaValidationError unless payload matches the contract."""
    _check(payload, load_schema(contract_name), contract_name, "$")
