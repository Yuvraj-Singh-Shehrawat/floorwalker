"""Rule-based planner. Used when there is no ANTHROPIC_API_KEY, and as the safety net when
an LLM call fails. It calls the same tools as the LLM and follows a fixed runbook:

  1. Config changed just before the break?            -> config_change, roll back
  2. Drop is mostly requests/DAU?                      -> traffic_drop, escalate (not an ad problem)
  3. One network much slower than normal?              -> network_latency, disable it in that geo
  4. One network's response rate or price collapsed?   -> demand_drop; waterfall: step floor down, bidder: escalate
  5. Otherwise                                         -> unknown, escalate

HONESTY NOTE: these rules were written alongside the five built-in incident types, so this
planner scores near-perfectly on them. Treat its eval numbers as a sanity check of the
plumbing, not as evidence the system generalises. Report LLM-mode numbers.
"""
from __future__ import annotations

from collections import defaultdict

from ..domain import BIDDERS
from .tools import ToolContext, normalize_proposal


def focus_geo(signals: list[dict]) -> str | None:
    """Geo carrying the most anomaly weight (sum of |z|) among the detector's signals."""
    weight: dict[str, float] = defaultdict(float)
    for s in signals:
        weight[s["geo"]] += abs(s["z"])
    return max(weight, key=weight.get) if weight else None


def investigate(ctx: ToolContext) -> dict:
    signals = ctx.incident["signals"]
    geo = focus_geo(signals)

    # 1. config
    cfg = ctx.call("get_config_changes", {"lookback_minutes": 60})
    for change in cfg.get("recent_changes", []):
        geos = {c["geo"] for c in change["changes"]}
        if geo is None or geo in geos:
            nets = {c["network"] for c in change["changes"]}
            sections = sorted({c["section"] for c in change["changes"]})
            return normalize_proposal({
                "category": "config_change", "geo": geo if geo in geos else None,
                "network": next(iter(nets)) if len(nets) == 1 else None,
                "explanation": (f"Config v{change['version']} was pushed by {change['actor']} "
                                f"{change['minutes_before_detection']} sim-min before detection "
                                f"(\"{change['note']}\") and changed {', '.join(sections)} in the affected geo. "
                                f"Metrics broke right after."),
                "action_type": "rollback_config", "to_version": change["rollback_target"], "confidence": 0.9})

    # 2. traffic
    dec = ctx.call("decompose_revenue", {"geo": geo} if geo else {})
    comps = {c["component"]: c for c in dec.get("components", [])}
    req = comps.get("requests_per_dau", {})
    if req.get("change_pct", 0) < -15 and req.get("share_of_change_pct", 0) > 60:
        return normalize_proposal({
            "category": "traffic_drop", "geo": geo,
            "explanation": (f"Requests per DAU in {geo} are {req['change_pct']}% vs baseline and explain "
                            f"{req['share_of_change_pct']}% of the revenue change; fill, show rate and eCPM are normal. "
                            f"The ad stack is healthy; fewer players are reaching ad placements."),
            "action_type": "escalate", "escalate_to": "game / LiveOps on-call", "confidence": 0.85})

    # 3 + 4. per-network symptoms
    health = ctx.call("network_health", {"geo": geo}) if geo else {"networks": []}
    nets = health.get("networks", [])

    def ratio(row: dict, key: str) -> float:
        r = row[key]["ratio"]
        return 1.0 if r is None else r

    slow = [n for n in nets if ratio(n, "avg_latency_ms") >= 2.0]
    if slow:
        n = max(slow, key=lambda r: ratio(r, "avg_latency_ms"))
        return normalize_proposal({
            "category": "network_latency", "geo": geo, "network": n["network"],
            "explanation": (f"{n['network']} latency in {geo} is {ratio(n, 'avg_latency_ms'):.1f}x baseline with a "
                            f"{n['timeout_rate']['actual']:.0%} timeout rate, and no config change explains it. "
                            f"Slow auctions are costing show rate for every network in the geo."),
            "action_type": "disable_network", "confidence": 0.8})

    weak = [n for n in nets if min(ratio(n, "response_rate"), ratio(n, "avg_price_cpm")) <= 0.75]
    if weak:
        n = min(weak, key=lambda r: min(ratio(r, "response_rate"), ratio(r, "avg_price_cpm")))
        symptom = (f"response rate {ratio(n, 'response_rate'):.2f}x and average price "
                   f"{ratio(n, 'avg_price_cpm'):.2f}x baseline")
        if n["network"] in BIDDERS:
            return normalize_proposal({
                "category": "demand_drop", "geo": geo, "network": n["network"],
                "explanation": (f"{n['network']} in {geo}: {symptom}; latency normal, no config change. "
                                f"Demand-side change at the bidder. Bidders have no floor to tune, so there is no "
                                f"safe config fix."),
                "action_type": "escalate", "escalate_to": f"{n['network']} account manager", "confidence": 0.7})
        return normalize_proposal({
            "category": "demand_drop", "geo": geo, "network": n["network"],
            "explanation": (f"{n['network']} in {geo}: {symptom}; latency normal, no config change. Its valuations "
                            f"fell below the configured floor. Step the floor down and let the canary decide."),
            "action_type": "set_floor", "floor_change_pct": -15, "confidence": 0.6})

    return normalize_proposal({
        "category": "unknown", "geo": geo,
        "explanation": "No config change, traffic shift or single-network symptom explains the anomaly.",
        "action_type": "escalate", "escalate_to": "ad-ops on-call", "confidence": 0.2})
