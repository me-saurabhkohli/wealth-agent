"""Run tracing: the flight recorder.

The article declares observability in the blueprint and consumes it through
a trace event bus with three pieces: TraceEventBus, JsonlExporter, and
CostTracker. Those names exist in the real pyagent-trace package (verified
on PyPI, version 0.3.0). This module is a small local implementation with
the same names and semantics, so the project runs with no extra dependency
and a finished run can be replayed step by step from its JSONL file.

Every event carries the token count of the step that produced it.
Deterministic steps (tax math, compliance rules) record zero tokens,
because there is no model call in them to pay for.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class TraceEvent:
    seq: int
    kind: str          # run, plan, worker, adapter, model, gate, synthesis
    name: str          # e.g. market.fetch, tax.optimize, compliance.check
    detail: str = ""
    tokens: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class TraceEventBus:
    """Publishes each step of a run to every subscriber, in order."""

    def __init__(self) -> None:
        self._subscribers = []
        self._seq = 0
        self.events: list[TraceEvent] = []

    def subscribe(self, subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, kind: str, name: str, detail: str = "", tokens: int = 0) -> TraceEvent:
        self._seq += 1
        event = TraceEvent(seq=self._seq, kind=kind, name=name, detail=detail, tokens=tokens)
        self.events.append(event)
        for subscriber in self._subscribers:
            subscriber(event)
        return event


class JsonlExporter:
    """Writes one JSON object per event to a .jsonl file."""

    def __init__(self, path: str) -> None:
        self.path = path
        open(path, "w").close()

    def __call__(self, event: TraceEvent) -> None:
        with open(self.path, "a") as fh:
            fh.write(json.dumps(event.to_dict()) + "\n")


class CostTracker:
    """Totals tokens across the run. Cost per step stays attached to the
    step's own event; this tracker keeps the running total the article
    describes when it says the trace shows where the money went."""

    def __init__(self) -> None:
        self.total_tokens = 0
        self.by_name: dict[str, int] = {}

    def __call__(self, event: TraceEvent) -> None:
        self.total_tokens += event.tokens
        self.by_name[event.name] = self.by_name.get(event.name, 0) + event.tokens


def replay(path: str) -> list[dict]:
    """Read a recorded run back, step by step, in the order it happened."""
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def format_replay(events: list[dict]) -> str:
    lines = []
    for e in events:
        tok = f" [{e['tokens']} tokens]" if e.get("tokens") else ""
        lines.append(f"{e['seq']:>3}. {e['kind']:<9} {e['name']}{tok}  {e.get('detail', '')}")
    return "\n".join(lines)
