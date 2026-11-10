"""Policy-as-code guardrails. Every proposed action is checked here before it touches traffic."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .configstore import ConfigStore
from .domain import FORMATS, GEOS, NETWORKS, WATERFALL, slice_key

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.yaml"
ACTION_TYPES = ("rollback_config", "set_floor", "disable_network", "escalate")


@dataclass
class Decision:
    allowed: bool
    needs_approval: bool = False
    checks: list[dict] = field(default_factory=list)   # [{rule, passed, detail}]

    @property
    def violations(self) -> list[str]:
        return [c["detail"] for c in self.checks if not c["passed"]]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "needs_approval": self.needs_approval, "checks": self.checks}


class Policy:
    def __init__(self, data: dict) -> None:
        self.data = data

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY_PATH) -> "Policy":
        with open(path) as fh:
            return cls(yaml.safe_load(fh))

    @property
    def kill_switch(self) -> bool:
        return bool(self.data.get("kill_switch", False))

    @kill_switch.setter
    def kill_switch(self, value: bool) -> None:
        self.data["kill_switch"] = bool(value)

    @property
    def canary(self) -> dict:
        return self.data["canary"]

    def evaluate(self, action: dict, cfg: ConfigStore, tick: int, recent_agent_changes: int,
                 network_revenue_share: float | None = None) -> Decision:
        checks: list[dict] = []
        needs_approval = False

        def check(rule: str, passed: bool, detail: str) -> None:
            checks.append({"rule": rule, "passed": bool(passed), "detail": detail})

        kind = action.get("type")
        check("allowed_action", kind in self.data["allowed_actions"] and kind in ACTION_TYPES,
              f"action type {kind!r}")
        if kind == "escalate" or not checks[-1]["passed"]:
            return Decision(allowed=all(c["passed"] for c in checks), checks=checks)

        check("kill_switch", not self.kill_switch,
              "kill switch is ON: agent may not change config" if self.kill_switch else "kill switch is off")
        limit = self.data["rate_limit"]["max_changes_per_sim_hour"]
        check("rate_limit", recent_agent_changes < limit,
              f"{recent_agent_changes} agent changes in the last sim-hour (limit {limit})")

        current = cfg.current()
        if kind == "rollback_config":
            to_v = action.get("to_version")
            exists = isinstance(to_v, int) and any(v["version"] == to_v for v in cfg.versions)
            check("rollback_target", exists and to_v < cfg.version,
                  f"target version {to_v} (live is {cfg.version})")
            if checks[-1]["passed"]:
                lookback = self.data["rollback"]["max_lookback_sim_min"]
                undone = [v for v in cfg.versions if v["version"] > to_v]
                oldest = min(v["tick"] for v in undone)
                check("rollback_lookback", tick - oldest <= lookback,
                      f"undoes {len(undone)} change(s), oldest {tick - oldest} sim-min ago (limit {lookback})")
        elif kind == "set_floor":
            geo, net, pct = action.get("geo"), action.get("network"), action.get("change_pct")
            ok = geo in GEOS and net in WATERFALL and isinstance(pct, (int, float))
            check("floor_target", ok, f"{net} in {geo}, change {pct}% (floors exist on waterfall networks only)")
            if ok:
                step = self.data["floor"]["max_step_pct"]
                check("floor_step", abs(pct) <= step + 1e-9 and pct > -100,
                      f"floor change {pct:+.1f}% (max step +/-{step}%)")
        elif kind == "disable_network":
            geo, net = action.get("geo"), action.get("network")
            ok = geo in GEOS and net in NETWORKS
            check("disable_target", ok, f"{net} in {geo}")
            if ok:
                left = min(sum(1 for n, on in current["enabled"][slice_key(geo, f)].items() if on and n != net)
                           for f in FORMATS)
                check("keep_demand", left >= 2, f"{left} other networks stay enabled in {geo}")
                thr = self.data["network_disable"]["approval_revenue_share"]
                share = network_revenue_share or 0.0
                needs_approval = share > thr
                checks.append({"rule": "revenue_share_approval", "passed": True,
                               "detail": f"{net} normally earns {share:.0%} of {geo} revenue; "
                                         + (f"above {thr:.0%}, human approval required" if needs_approval
                                            else f"below {thr:.0%}, no approval needed")})
        allowed = all(c["passed"] for c in checks)
        return Decision(allowed=allowed, needs_approval=needs_approval and allowed, checks=checks)


def build_candidate(action: dict, cfg: ConfigStore) -> dict:
    """The config that would be live if `action` were applied. Does not change anything."""
    kind = action["type"]
    if kind == "rollback_config":
        return cfg.get(int(action["to_version"]))
    new = copy.deepcopy(cfg.current())
    geo, net = action["geo"], action["network"]
    for f in FORMATS:
        k = slice_key(geo, f)
        if kind == "set_floor":
            new["floors"][k][net] = round(new["floors"][k][net] * (1 + action["change_pct"] / 100.0), 4)
        elif kind == "disable_network":
            new["enabled"][k][net] = False
    return new


def describe_action(action: dict) -> str:
    kind = action.get("type")
    if kind == "rollback_config":
        return f"Roll back mediation config to version {action.get('to_version')}"
    if kind == "set_floor":
        return f"Change {action.get('network')} floors in {action.get('geo')} by {action.get('change_pct'):+.0f}%"
    if kind == "disable_network":
        return f"Disable {action.get('network')} in {action.get('geo')}"
    if kind == "escalate":
        return f"Escalate to {action.get('team', 'ad-ops on-call')}: no automatic change"
    return f"Unknown action {kind!r}"
