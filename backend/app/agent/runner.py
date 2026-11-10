"""Incident lifecycle: open -> investigate -> policy -> (approval) -> canary -> promote/revert -> postmortem.

IncidentManager.handle() is blocking. The API server runs it in a background thread while the
sim clock keeps ticking; evals run it inline with a HeadlessClock that advances the world itself.

Statuses: investigating, needs_approval, canary, verifying, then one of
          resolved (fix promoted), reverted (canary lost, nothing promoted), escalated (handed to a human).
"""
from __future__ import annotations

import itertools
import logging
import os
import threading
import time

from ..configstore import diff_configs
from ..domain import NET_INDEX, N_REV, SLICES, S_REV, sim_time_label
from ..policy import Policy, build_candidate, describe_action
from ..tracing import Tracer
from . import fallback
from .tools import ToolContext

log = logging.getLogger("floorwalker.agent")
AGENT_ACTOR = "floorwalker-agent"
CLOSED = ("resolved", "reverted", "escalated")
VERIFY_TICKS = 10

FOLLOW_UPS = {
    "config_change": "Add a pre-push validator: reject timeouts under 300 ms and floor moves over 2x in one push.",
    "network_latency": "Open a ticket with the network. Re-enable it behind a canary once its latency is back under the timeout.",
    "demand_drop": "Ask the network's account manager what changed on the demand side; re-check floors in 24 hours.",
    "traffic_drop": "Page game / LiveOps on-call: check the latest release, store status and UA campaigns for this geo.",
    "unknown": "Human review needed: the standard runbook did not isolate a cause.",
}


class LiveClock:
    """Server mode: the tick loop advances the world; we just wait for it."""

    def __init__(self, world) -> None:
        self.world = world

    def wait(self, ticks: int, until=None) -> None:
        target = self.world.tick + ticks
        while self.world.tick < target:
            if until and until():
                return
            time.sleep(0.1)

    def think(self) -> None:   # real LLM latency already passes in sim time
        pass


class HeadlessClock:
    """Eval mode: waiting means stepping the world. `think_ticks` models investigation time."""

    def __init__(self, world, think_ticks: int = 2) -> None:
        self.world = world
        self.think_ticks = think_ticks

    def wait(self, ticks: int, until=None) -> None:
        for _ in range(ticks):
            if until and until():
                return
            self.world.step()

    def think(self) -> None:
        self.world.run(self.think_ticks)


class IncidentManager:
    def __init__(self, world, policy: Policy, clock, mode: str | None = None, llm_client=None,
                 tracer: Tracer | None = None, auto_approve_after: int | None = None,
                 onset_lag: int = 2) -> None:
        self.world = world
        self.policy = policy
        self.clock = clock
        self.tracer = tracer or Tracer()
        self.llm_client = llm_client
        self.onset_lag = onset_lag                        # detector fires after (onset_lag + 1) breached minutes
        self.auto_approve_after = auto_approve_after      # evals: simulate a human approving after N ticks
        mode = (mode or os.getenv("FLOORWALKER_AGENT_MODE", "auto")).lower()
        has_key = bool(os.getenv("ANTHROPIC_API_KEY")) or llm_client is not None
        self.mode = "llm" if (mode == "llm" or (mode == "auto" and has_key)) else "fallback"
        self.incidents: list[dict] = []
        self.active: dict | None = None
        self._ids = itertools.count(1)
        self._approval: dict[str, bool] = {}
        self._llm = None
        self.on_close = None                              # optional callback(incident), set by the server
        self._lock = threading.Lock()

    # ---- helpers ----
    def _step(self, inc: dict, kind: str, title: str, detail=None) -> None:
        inc["steps"].append({"tick": self.world.tick, "time": sim_time_label(self.world.tick),
                             "kind": kind, "title": title, "detail": detail})

    def _geo_revenue_ratio(self, geo: str | None, t0: int, t1: int) -> float:
        idx = [i for i, (g, _f) in enumerate(SLICES) if geo is None or g == geo]
        with self.world.lock:
            a, _ = self.world.metrics.window(t0, t1)
            e, _ = self.world.baseline.window(t0, t1)
        return float(a[idx, S_REV].sum() / max(e[idx, S_REV].sum(), 1e-12))

    def _network_share(self, geo: str, network: str) -> float:
        idx = [i for i, (g, _f) in enumerate(SLICES) if g == geo]
        rev = self.world.baseline.net[:, idx, :, N_REV].sum(axis=(0, 1))
        return float(rev[NET_INDEX[network]] / max(rev.sum(), 1e-12))

    def _recent_agent_changes(self) -> int:
        t = self.world.tick
        return sum(1 for v in self.world.cfg.versions if v["actor"] == AGENT_ACTOR and v["tick"] >= t - 60)

    # ---- lifecycle ----
    def open(self, signals: list[dict]) -> dict:
        t = self.world.tick
        focus = fallback.focus_geo(signals)
        inc = {
            "id": f"INC-{next(self._ids):04d}", "status": "investigating", "mode": self.mode,
            "opened_tick": t, "opened_label": sim_time_label(t), "closed_tick": None,
            "onset_tick": t - self.onset_lag,
            "signals": signals, "focus_geo": focus,
            "impact": {"total_change_pct": round((self._geo_revenue_ratio(None, t - 4, t) - 1) * 100, 1),
                       "focus_geo_change_pct": round((self._geo_revenue_ratio(focus, t - 4, t) - 1) * 100, 1)},
            "steps": [], "root_cause": None, "action": None, "policy": None, "canary": None,
            "recovery": None, "postmortem": None, "usage": None,
        }
        with self._lock:
            self.incidents.append(inc)
            self.incidents[:] = self.incidents[-50:]
            self.active = inc
        self._step(inc, "system", "Incident opened by anomaly detector",
                   {"signals": signals[:6], "impact": inc["impact"]})
        return inc

    def approve(self, incident_id: str, approved: bool) -> bool:
        with self._lock:
            if self.active and self.active["id"] == incident_id and self.active["status"] == "needs_approval":
                self._approval[incident_id] = approved
                return True
        return False

    def handle(self, inc: dict) -> dict:
        try:
            with self.tracer.span(f"incident {inc['id']}", as_type="agent",
                                  input={"signals": inc["signals"], "impact": inc["impact"]}) as root:
                self._handle(inc)
                root.update(output={"status": inc["status"], "root_cause": inc["root_cause"],
                                    "action": inc["action"], "canary": inc["canary"]})
        except Exception as exc:   # never leave an incident stuck open
            log.exception("incident handling crashed")
            self.world.end_canary()
            self._step(inc, "system", "Agent error; escalated to a human", str(exc))
            inc["status"] = "escalated"
        finally:
            inc["closed_tick"] = self.world.tick
            if self.on_close:
                self.on_close(inc)          # runs BEFORE the incident stops being active (no detection gap)
            with self._lock:
                self.active = None
            threading.Thread(target=self.tracer.flush, daemon=True).start()   # never block on telemetry
        return inc

    def _investigate(self, inc: dict) -> dict:
        def on_call(name: str, args: dict, out: dict) -> None:
            with self.tracer.span(name, as_type="tool", input=args) as obs:
                obs.update(output=out)
            self._step(inc, "tool", name, {"input": args, "output": out})

        ctx = ToolContext(self.world, inc, on_call=on_call)
        if self.mode == "llm":
            try:
                from .llm import LLMInvestigator
                llm = LLMInvestigator(client=self.llm_client, tracer=self.tracer,
                                      on_thought=lambda text: self._step(inc, "thought", text))
                self._llm = llm
                proposal = llm.investigate(ctx)
                inc["usage"] = llm.usage
                return proposal
            except Exception as exc:   # noqa: BLE001 - any LLM failure falls back to the runbook
                log.warning("LLM investigation failed, using fallback planner: %s", exc)
                self._step(inc, "system", "LLM call failed; switching to the rule-based runbook",
                           f"{type(exc).__name__}: {exc}"[:300])
                inc["mode"] = "fallback"
        self._llm = None
        return fallback.investigate(ctx)

    def _handle(self, inc: dict) -> None:
        w = self.world
        proposal = self._investigate(inc)
        self.clock.think()
        inc["root_cause"], action = proposal["root_cause"], proposal["action"]
        inc["confidence"] = proposal["confidence"]
        inc["action"] = action
        self._step(inc, "decision", f"Root cause: {inc['root_cause']['category']}", inc["root_cause"])
        self._step(inc, "decision", f"Proposed action: {describe_action(action)}", action)

        share = None
        if action["type"] == "disable_network":
            share = self._network_share(action["geo"], action["network"])
        decision = self.policy.evaluate(action, w.cfg, w.tick, self._recent_agent_changes(), share)
        inc["policy"] = decision.to_dict()
        self._step(inc, "policy", "Policy check " + ("passed" if decision.allowed else "BLOCKED the action"),
                   decision.checks)

        if not decision.allowed:
            self._escalate(inc, "Policy blocked the proposed action: " + "; ".join(decision.violations))
            return
        if action["type"] == "escalate":
            self._escalate(inc, f"No safe automatic fix. Escalated to {action.get('team')}.")
            return
        if decision.needs_approval and not self._wait_for_approval(inc):
            return

        # ---- canary ----
        pre_ratio = self._geo_revenue_ratio(inc["root_cause"].get("geo") or inc["focus_geo"], inc["onset_tick"], w.tick)
        candidate = build_candidate(action, w.cfg)
        can_cfg = self.policy.canary
        with w.lock:
            n_changes = len(diff_configs(w.cfg.current(), candidate))
            if n_changes == 0:
                self._escalate(inc, "The proposed action would not change the live config.")
                return
            w.start_canary(inc["id"], candidate, float(can_cfg["fraction"]))
        inc["status"] = "canary"
        self._step(inc, "canary", f"Canary started on {can_cfg['fraction']:.0%} of affected traffic "
                                  f"for {can_cfg['duration_sim_min']} sim-min", {"config_values_changed": n_changes})
        self.clock.wait(int(can_cfg["duration_sim_min"]))
        can = w.end_canary()
        c_rpr, t_rpr = can.rpr()
        ratio = float(t_rpr / c_rpr) if c_rpr > 0 else 0.0
        promote = bool(ratio >= float(can_cfg["min_rpr_ratio"]))
        inc["canary"] = {"control_rpr": round(c_rpr * 1000, 4), "canary_rpr": round(t_rpr * 1000, 4),
                         "unit": "USD per 1000 requests", "ratio": round(ratio, 3),
                         "lift_pct": round((ratio - 1) * 100, 1), "promoted": promote,
                         "duration_sim_min": int(can_cfg["duration_sim_min"])}
        if not promote:
            inc["status"] = "reverted"
            self._step(inc, "canary", f"Canary lost ({(ratio - 1) * 100:+.1f}% revenue/request vs control). "
                                      f"Reverted; nothing was promoted. Escalating.", inc["canary"])
            self._postmortem(inc)
            return
        with w.lock:
            version = w.cfg.push(candidate, actor=AGENT_ACTOR, tick=w.tick,
                                 note=f"{inc['id']}: {describe_action(action)}")
        self._step(inc, "canary", f"Canary won ({(ratio - 1) * 100:+.1f}% revenue/request vs control). "
                                  f"Promoted to 100% as config v{version}.", inc["canary"])

        # ---- verify ----
        inc["status"] = "verifying"
        promoted_at = w.tick
        self.clock.wait(VERIFY_TICKS)
        geo = inc["root_cause"].get("geo") or inc["focus_geo"]
        post_ratio = self._geo_revenue_ratio(geo, promoted_at + 1, w.tick)
        inc["recovery"] = {"geo": geo or "all", "before_fix_vs_baseline_pct": round((pre_ratio - 1) * 100, 1),
                           "after_fix_vs_baseline_pct": round((post_ratio - 1) * 100, 1),
                           "minutes_detect_to_fix_live": int(promoted_at - inc["opened_tick"])}
        inc["status"] = "resolved"
        self._step(inc, "system", f"Verified: {geo or 'portfolio'} revenue went from "
                                  f"{(pre_ratio - 1) * 100:+.1f}% to {(post_ratio - 1) * 100:+.1f}% vs baseline",
                   inc["recovery"])
        self._postmortem(inc)

    def _wait_for_approval(self, inc: dict) -> bool:
        timeout = int(self.policy.data["network_disable"]["approval_timeout_sim_min"])
        inc["status"] = "needs_approval"
        self._step(inc, "policy", f"Waiting for human approval (auto-expires in {timeout} sim-min)")
        if self.auto_approve_after is not None:
            self.clock.wait(self.auto_approve_after)
            self._approval[inc["id"]] = True
        else:
            self.clock.wait(timeout, until=lambda: inc["id"] in self._approval)
        verdict = self._approval.pop(inc["id"], None)
        if verdict:
            self._step(inc, "policy", "Human approved the action")
            return True
        self._escalate(inc, "Human rejected the action." if verdict is False
                       else "Approval timed out; no change was made.")
        return False

    def _escalate(self, inc: dict, reason: str) -> None:
        inc["status"] = "escalated"
        self._step(inc, "system", reason)
        self._postmortem(inc)

    def _postmortem(self, inc: dict) -> None:
        rc, action = inc["root_cause"] or {}, inc["action"] or {}
        facts = {
            "incident": inc["id"], "status": inc["status"], "detected": inc["opened_label"],
            "impact_at_detection": inc["impact"],
            "top_signals": inc["signals"][:3],
            "root_cause": rc, "action": describe_action(action) if action else None,
            "canary": inc["canary"], "recovery": inc["recovery"],
            "follow_up": FOLLOW_UPS.get(rc.get("category", "unknown"), FOLLOW_UPS["unknown"]),
        }
        narrative = None
        if getattr(self, "_llm", None) is not None and os.getenv("FLOORWALKER_LLM_POSTMORTEM", "1") == "1":
            try:
                narrative = self._llm.write_narrative(facts)
                inc["usage"] = self._llm.usage
            except Exception as exc:   # noqa: BLE001
                log.warning("LLM postmortem failed, using template: %s", exc)
        if not narrative:
            narrative = self._template_narrative(inc, facts)
        inc["postmortem"] = {**facts, "narrative": narrative}
        self._step(inc, "system", "Postmortem written")

    @staticmethod
    def _template_narrative(inc: dict, facts: dict) -> str:
        rc = facts["root_cause"]
        where = " ".join(x for x in (rc.get("network"), f"in {rc['geo']}" if rc.get("geo") else None) if x)
        s = (f"{inc['id']} was detected at {facts['detected']} with {inc['focus_geo'] or 'portfolio'} revenue "
             f"{inc['impact']['focus_geo_change_pct']:+.1f}% vs baseline. "
             f"Root cause: {rc.get('category', 'unknown').replace('_', ' ')}"
             f"{(' (' + where + ')') if where else ''}. ")
        if inc["status"] == "resolved":
            r, c = facts["recovery"], facts["canary"]
            s += (f"{facts['action']} won a {c['duration_sim_min']}-minute canary by {c['lift_pct']:+.1f}% "
                  f"revenue/request and was promoted; revenue moved from {r['before_fix_vs_baseline_pct']:+.1f}% to "
                  f"{r['after_fix_vs_baseline_pct']:+.1f}% vs baseline.")
        elif inc["status"] == "reverted":
            s += (f"{facts['action']} lost its canary ({facts['canary']['lift_pct']:+.1f}% revenue/request), "
                  f"so nothing was promoted and the incident was escalated.")
        else:
            s += "No automatic change was made; the incident was escalated with the evidence attached."
        return s
