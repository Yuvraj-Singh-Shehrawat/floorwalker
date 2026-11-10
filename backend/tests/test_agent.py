"""The LLM loop is tested with a scripted fake client, so no API key or network is needed."""
import json
from types import SimpleNamespace as NS

from app import chaos
from app.agent.llm import LLMInvestigator
from app.agent.runner import HeadlessClock, IncidentManager
from app.agent.tools import ToolContext
from app.detection import Detector
from app.policy import Policy
from app.simulator import World


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create with a fixed script of responses."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs["messages"], default=lambda o: vars(o))))
        return self.script.pop(0)


def msg(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=100, output_tokens=20))


def text(t):
    return NS(type="text", text=t)


def tool(i, name, **inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


def open_incident(baseline, ctype, seed=3, **mgr_kwargs):
    w = World(seed=seed, start_tick=3000, baseline=baseline)
    det = Detector(baseline)
    mgr = IncidentManager(w, Policy.load(), HeadlessClock(w), auto_approve_after=5, **mgr_kwargs)
    for _ in range(20):
        w.step()
        det.check(w.metrics, w.tick)
    ev = chaos.inject(w, ctype, geo="US")
    for _ in range(30):
        w.step()
        signals = det.check(w.metrics, w.tick)
        if signals:
            return w, mgr, ev, mgr.open(signals)
    raise AssertionError("incident was not detected")


def test_fallback_resolves_a_bad_floor_push(baseline):
    w, mgr, ev, inc = open_incident(baseline, "bad_floor_push", mode="fallback")
    assert inc["opened_tick"] - ev.start_tick <= 6
    mgr.handle(inc)
    assert inc["status"] == "resolved"
    assert inc["root_cause"]["category"] == "config_change" and inc["root_cause"]["geo"] == "US"
    assert inc["action"] == {"type": "rollback_config", "to_version": 1}
    assert inc["canary"]["promoted"] and inc["canary"]["lift_pct"] > 10
    assert inc["recovery"]["after_fix_vs_baseline_pct"] > inc["recovery"]["before_fix_vs_baseline_pct"] + 15
    assert w.cfg.versions[-1]["actor"] == "floorwalker-agent" and w.canary is None
    json.dumps(inc)   # the whole incident must be JSON-serialisable for the API


def test_fallback_escalates_a_traffic_drop_without_touching_config(baseline):
    w, mgr, _ev, inc = open_incident(baseline, "traffic_drop", mode="fallback")
    mgr.handle(inc)
    assert inc["status"] == "escalated" and inc["root_cause"]["category"] == "traffic_drop"
    assert w.cfg.version == 1


def test_llm_loop_follows_the_tool_protocol(baseline):
    client = FakeClient([
        msg(text("Checking recent config pushes."), tool("t1", "get_config_changes")),
        msg(tool("t2", "decompose_revenue", geo="US"), tool("t3", "network_health", geo="US")),
        msg(tool("t4", "propose_fix", category="config_change", geo="US", explanation="v2 raised floors 5x.",
                 action_type="rollback_config", to_version=1, confidence=0.9)),
        msg(text("Summary."), stop="end_turn"),
    ])
    w, mgr, _ev, inc = open_incident(baseline, "bad_floor_push", llm_client=client)
    assert mgr.mode == "llm"
    mgr.handle(inc)
    assert inc["status"] == "resolved" and inc["mode"] == "llm"
    assert inc["usage"]["llm_calls"] == 4          # 3 investigation turns + postmortem
    # second request: assistant turn echoed, then one tool_result for t1
    second = client.calls[1]
    assert second[1]["role"] == "assistant"
    assert second[2]["content"][0]["type"] == "tool_result" and second[2]["content"][0]["tool_use_id"] == "t1"
    # third request: both tool results, in order
    assert [r["tool_use_id"] for r in client.calls[2][4]["content"]] == ["t2", "t3"]
    kinds = [s["kind"] for s in inc["steps"]]
    assert kinds.count("tool") == 3 and "thought" in kinds
    assert inc["postmortem"]["narrative"] == "Summary."


def test_invalid_proposal_is_returned_to_the_model_as_an_error(baseline):
    client = FakeClient([
        msg(tool("t1", "propose_fix", category="config_change", explanation="x", action_type="rollback_config")),
        msg(tool("t2", "propose_fix", category="config_change", geo="US", explanation="x",
                 action_type="rollback_config", to_version=1)),
    ])
    w, mgr, _ev, inc = open_incident(baseline, "bad_floor_push", llm_client=client)
    proposal = LLMInvestigator(client=client).investigate(ToolContext(w, inc))
    assert proposal["action"] == {"type": "rollback_config", "to_version": 1}
    err = client.calls[1][2]["content"][0]
    assert err["is_error"] and "to_version" in err["content"]


def test_llm_failure_falls_back_to_the_runbook(baseline):
    class Broken:
        messages = property(lambda self: self)

        def create(self, **_):
            raise RuntimeError("api down")

    _w, mgr, _ev, inc = open_incident(baseline, "bad_floor_push", llm_client=Broken())
    mgr.handle(inc)
    assert inc["status"] == "resolved" and inc["mode"] == "fallback"
    assert any("LLM call failed" in s["title"] for s in inc["steps"])


def test_policy_violation_from_the_llm_is_blocked(baseline):
    client = FakeClient([
        msg(tool("t1", "propose_fix", category="demand_drop", geo="US", network="Network D", explanation="x",
                 action_type="set_floor", floor_change_pct=-60)),
    ])
    w, mgr, _ev, inc = open_incident(baseline, "bad_floor_push", llm_client=client)
    v_before = w.cfg.version
    mgr.handle(inc)
    assert inc["status"] == "escalated" and not inc["policy"]["allowed"]
    assert w.cfg.version == v_before and w.canary is None


def test_tools_do_not_leak_ground_truth(baseline):
    w, _mgr, _ev, inc = open_incident(baseline, "network_latency_spike", mode="fallback")
    ctx = ToolContext(w, inc)
    blob = json.dumps([ctx.call("get_config_changes"), ctx.call("localize_revenue_change"),
                       ctx.call("decompose_revenue"), ctx.call("network_health", {"geo": "US"}),
                       ctx.call("query_metrics", {"geo": "US", "network": "Network A"})])
    assert "chaos" not in blob and "truth" not in blob and "network_latency_spike" not in blob
    assert "error" in ctx.call("network_health", {"geo": "ZZ"})
