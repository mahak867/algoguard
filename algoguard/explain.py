"""Explainer: a plain-language reason for every decision.

The default is a template built from the findings, which always works and needs no network. If an LLM is
configured, it may rewrite that text into one or two friendlier sentences, but only from the structured facts,
and its answer is thrown away unless every number in it appears in those facts. The explainer never makes a
decision; it describes one that the rules and models already made.
"""
from __future__ import annotations

import json
import re

import httpx

from algoguard.models import Decision, Finding, Outcome

_LEAD = {Outcome.PASS: "Sent", Outcome.HOLD: "Held for your confirmation", Outcome.BLOCK: "Blocked"}
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def template(d: Decision) -> str:
    s = d.signal
    order = f"{s.side} {s.size} {s.symbol}" + (f" at {s.price:g}" if s.order_type == "limit" and s.price else "")
    main = [f for f in d.findings if f.severity == ("block" if d.outcome is Outcome.BLOCK else "hold")]
    if d.outcome is Outcome.PASS:
        binding = next((f for f in d.findings if f.code == "binding_limit"), None)
        tail = f" {binding.message}" if binding else ""
        return f"{_LEAD[d.outcome]}: {order}. All your limits passed and nothing about the alert looked unusual.{tail}"
    reasons = " ".join(f.message for f in main[:3])
    more = f" (+{len(main) - 3} more)" if len(main) > 3 else ""
    return f"{_LEAD[d.outcome]}: {order}. {reasons}{more}"


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def grounded(text: str, facts_text: str) -> bool:
    """True if every number in `text` also appears in the facts (so the LLM invented none)."""
    allowed = _numbers(facts_text)
    return all(n in allowed for n in _numbers(text))


class Explainer:
    def __init__(self, api_key: str | None = None, model: str = "claude-haiku-5-5", timeout: float = 3.0,
                 client: httpx.Client | None = None):
        self.api_key, self.model = api_key, model
        self.client = client or httpx.Client(timeout=timeout)

    def explain(self, d: Decision, use_llm: bool = False) -> str:
        base = template(d)
        if not (use_llm and self.api_key):
            return base
        facts = {"outcome": d.outcome.value, "order": {"side": d.signal.side, "size": d.signal.size,
                 "symbol": d.signal.symbol, "price": d.signal.price},
                 "findings": [{"code": f.code, "message": f.message, **f.facts} for f in d.findings]}
        facts_text = json.dumps(facts)
        try:
            text = self._ask(facts_text, base)
        except Exception:
            return base
        return text if text and grounded(text, facts_text + " " + base) else base

    def _ask(self, facts_text: str, base: str) -> str:
        r = self.client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 150, "messages": [{"role": "user", "content": (
                "Rewrite this trading-safety decision for a retail trader in at most two short sentences. "
                "Use only the facts given; do not add numbers, advice or predictions.\n"
                f"Facts: {facts_text}\nCurrent text: {base}")}]},
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()


def finding_text(f: Finding) -> str:
    return f"[{f.source}/{f.code}] {f.message}"
