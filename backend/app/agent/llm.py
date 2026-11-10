"""LLM investigator: Claude with tool calling, via the official `anthropic` SDK.

This file is the complete, working tool-use loop. You do not need the SDK docs to extend it:

  * `client.messages.create(model=, max_tokens=, system=, tools=, messages=)` returns a message.
  * `resp.content` is a list of blocks. `block.type == "text"` has `.text`;
    `block.type == "tool_use"` has `.id`, `.name`, `.input` (a dict).
  * `resp.stop_reason == "tool_use"` means Claude is waiting for tool results.
  * Reply by appending the assistant turn (`resp.content` as-is) and then ONE user turn whose
    content is a list of `{"type": "tool_result", "tool_use_id": <id>, "content": <string>}`,
    one per tool_use block, in the same order.
  * Token counts are in `resp.usage.input_tokens` / `resp.usage.output_tokens`.

To add a tool: add its schema to TOOL_SCHEMAS in tools.py and a `tool_<name>` method on ToolContext.
"""
from __future__ import annotations

import json
import os
from typing import Callable

from .tools import TOOL_SCHEMAS, ToolContext, normalize_proposal

DEFAULT_MODEL = os.getenv("FLOORWALKER_MODEL", "claude-haiku-5-5")
MAX_STEPS = 8

SYSTEM_PROMPT = """You are Floorwalker, the on-call ad-monetization agent for a mobile game portfolio.
An anomaly detector has opened a revenue incident. Find the root cause and propose ONE action.

The ad stack: hybrid mediation. Networks A, B, C are in-app bidders (first-price). Networks D, E, F are
waterfall lines with price floors, walked from highest floor to lowest. Slow auctions lose impressions
(show rate falls with latency). Geos: IN, US, BR, ID, DE. Formats: rewarded, interstitial, banner.

How to work:
- Tools do all arithmetic. Never state a number that did not come from a tool result.
- Typical order: get_config_changes -> decompose_revenue (which lever) -> localize_revenue_change /
  network_health (where and what symptom). Stop as soon as the evidence is sufficient; 3-5 tool calls is normal.
- Before each tool call, write one short sentence saying what you are checking and why.
- A config push shortly before detection that touches the affected scope is the most likely cause.
  Distinguish it from external causes (a network got slow, demand fell, traffic fell) where no config changed.

Actions (all pass policy checks and a 10% canary before going live):
- rollback_config: restore the version before a bad push. Use the tool's rollback_target.
- disable_network: stop calling one network in one geo, e.g. when it is timing out and dragging auctions.
- set_floor: move one WATERFALL network's floor in one geo by at most +/-15%.
- escalate: no config change. Correct when the cause is outside the ad stack (traffic), when a bidder's
  demand fell (bidders have no floor), or when you are not confident. Escalating is a good outcome, not a failure.

Finish by calling propose_fix exactly once. Keep the explanation to 2-3 sentences that cite the evidence."""


def incident_brief(incident: dict) -> str:
    lines = [f"Incident {incident['id']} opened at {incident['opened_label']}.",
             f"Portfolio revenue vs baseline (last 5 sim-min): {incident['impact']['total_change_pct']}%.",
             "Detector signals (most anomalous first; change is vs seasonal baseline):"]
    for s in incident["signals"][:8]:
        net = f" {s['network']}" if s["network"] else ""
        lines.append(f"- {s['series']}{net} in {s['geo']}|{s['format']}: {s['change_pct']}% (z={s['z']})")
    lines.append("Investigate with the tools, then call propose_fix.")
    return "\n".join(lines)


class LLMInvestigator:
    def __init__(self, client=None, model: str = DEFAULT_MODEL,
                 on_thought: Callable[[str], None] | None = None, tracer=None) -> None:
        if client is None:
            import anthropic   # imported lazily so the fallback path has no hard dependency
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.on_thought = on_thought
        self.tracer = tracer
        self.usage = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}

    def _create(self, name: str, **kwargs):
        resp = None
        if self.tracer:
            with self.tracer.span(name, as_type="generation", model=self.model,
                                  input=kwargs["messages"][-1]["content"]) as obs:
                resp = self.client.messages.create(model=self.model, **kwargs)
                obs.update(output=[getattr(b, "text", None) or {"tool": getattr(b, "name", None),
                                                                  "input": getattr(b, "input", None)}
                                   for b in resp.content],
                           usage_details={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens})
        else:
            resp = self.client.messages.create(model=self.model, **kwargs)
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        self.usage["llm_calls"] += 1
        return resp

    def investigate(self, ctx: ToolContext) -> dict:
        """Run the tool-use loop until propose_fix. Raises RuntimeError if no valid proposal emerges."""
        messages: list[dict] = [{"role": "user", "content": incident_brief(ctx.incident)}]
        nudged = False
        for step in range(MAX_STEPS):
            resp = self._create(f"investigate-step-{step + 1}", max_tokens=1024, system=SYSTEM_PROMPT,
                                tools=TOOL_SCHEMAS, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "text" and block.text.strip() and self.on_thought:
                    self.on_thought(block.text.strip())
                if block.type != "tool_use":
                    continue
                if block.name == "propose_fix":
                    try:
                        return normalize_proposal(dict(block.input))
                    except ValueError as exc:   # let the model correct its own input
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": f"Invalid proposal: {exc}", "is_error": True})
                        continue
                out = ctx.call(block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, separators=(",", ":"))})
            if results:
                messages.append({"role": "user", "content": results})
            elif not nudged:
                nudged = True
                messages.append({"role": "user", "content": "Call propose_fix now with your conclusion."})
            else:
                break
        raise RuntimeError(f"LLM did not produce a valid proposal within {MAX_STEPS} steps")

    def write_narrative(self, facts: dict) -> str:
        """Three-sentence postmortem summary. Numbers come from `facts`; the model only words it."""
        resp = self._create("postmortem", max_tokens=300, messages=[{
            "role": "user",
            "content": ("Write a 3-sentence incident postmortem summary for an ad-ops channel. Plain prose, no "
                        "headers, no bullet points. Use ONLY the numbers in this JSON; do not invent any.\n\n"
                        + json.dumps(facts, separators=(",", ":")))}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()
