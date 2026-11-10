"""The agent's tools. Each one is a thin wrapper over deterministic code in rca.py / configstore.py.

The LLM never computes a number itself: it picks a tool, reads the JSON, and decides what
to look at next. The same tools are used by the rule-based fallback planner, so both
modes leave the same kind of trail on the incident timeline.

Nothing in this file reads chaos ground truth.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from .. import rca
from ..domain import (DAU, FORMATS, GEOS, NETWORKS, NET_INDEX, N_ATT, N_BID, N_LAT, N_RESP, N_REV,
                      SLICES, S_FILL, S_IMP, S_REQ, S_REV, sim_time_label)

MAX_ANALYSIS_WINDOW = 15  # analyse from incident onset to now, capped at this many sim-minutes
CATEGORIES = ("config_change", "network_latency", "demand_drop", "traffic_drop", "unknown")
AGENT_ACTORS = ("floorwalker-agent", "demo-reset", "system")

TOOL_SCHEMAS = [
    {
        "name": "decompose_revenue",
        "description": ("Exact log decomposition of the revenue-per-DAU change into requests/DAU, fill rate, "
                        "show rate and eCPM, from incident onset to now versus the seasonal baseline. "
                        "Use it to learn WHICH lever moved. Optionally scope to one geo and/or format."),
        "input_schema": {"type": "object", "properties": {
            "geo": {"type": "string", "enum": GEOS}, "format": {"type": "string", "enum": FORMATS}}},
    },
    {
        "name": "localize_revenue_change",
        "description": ("Adtributor-style localization: for the dimensions network, geo and format, which elements "
                        "explain the revenue change (explanatory power) and how unexpected each is (surprise). "
                        "Also returns the network x geo cells with the largest revenue loss. Use it to learn WHERE."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "network_health",
        "description": ("Per-network vital signs in one geo versus baseline: response rate (bid rate for bidders, "
                        "fill rate for waterfall lines), average price, latency, timeout rate, revenue. "
                        "A ratio far from 1.0 marks the sick network and the symptom."),
        "input_schema": {"type": "object", "properties": {
            "geo": {"type": "string", "enum": GEOS}, "format": {"type": "string", "enum": FORMATS}},
            "required": ["geo"]},
    },
    {
        "name": "get_config_changes",
        "description": ("Mediation config versions pushed shortly before the incident, with who pushed them, their "
                        "note and a diff summary. A config push just before a metric break is the most common root "
                        "cause in ad ops. `rollback_target` is the version to restore to undo a change."),
        "input_schema": {"type": "object", "properties": {
            "lookback_minutes": {"type": "integer", "minimum": 10, "maximum": 180}}},
    },
    {
        "name": "query_metrics",
        "description": ("Time series in 5-sim-minute buckets, actual versus baseline. Slice level: revenue, fill rate, "
                        "eCPM. Add `network` for that network's response rate, average price and latency."),
        "input_schema": {"type": "object", "properties": {
            "geo": {"type": "string", "enum": GEOS}, "format": {"type": "string", "enum": FORMATS},
            "network": {"type": "string", "enum": NETWORKS},
            "last_minutes": {"type": "integer", "minimum": 10, "maximum": 60}}},
    },
    {
        "name": "propose_fix",
        "description": ("Finish the investigation. State the root cause and ONE action. The action is checked "
                        "against policy, canaried on 10% of traffic and auto-reverted if it does not help. "
                        "Choose `escalate` when no safe config change addresses the cause."),
        "input_schema": {"type": "object", "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "geo": {"type": "string", "enum": GEOS, "description": "Affected geo, if localized"},
            "network": {"type": "string", "enum": NETWORKS, "description": "Affected network, if any"},
            "explanation": {"type": "string", "description": "2-3 sentences citing the tool evidence"},
            "action_type": {"type": "string", "enum": ["rollback_config", "set_floor", "disable_network", "escalate"]},
            "to_version": {"type": "integer", "description": "rollback_config: version to restore"},
            "floor_change_pct": {"type": "number", "description": "set_floor: percent change, max +/-15"},
            "escalate_to": {"type": "string", "description": "escalate: which team should act"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
            "required": ["category", "explanation", "action_type"]},
    },
]


def normalize_proposal(args: dict) -> dict:
    """Validate propose_fix input and turn it into {root_cause, action, confidence}."""
    cat = args.get("category")
    if cat not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    geo, net = args.get("geo"), args.get("network")
    if geo is not None and geo not in GEOS:
        raise ValueError(f"geo must be one of {GEOS}")
    if net is not None and net not in NETWORKS:
        raise ValueError(f"network must be one of {NETWORKS}")
    kind = args.get("action_type")
    action: dict = {"type": kind}
    if kind == "rollback_config":
        if not isinstance(args.get("to_version"), int):
            raise ValueError("rollback_config needs integer to_version")
        action["to_version"] = args["to_version"]
    elif kind == "set_floor":
        if geo is None or net is None or not isinstance(args.get("floor_change_pct"), (int, float)):
            raise ValueError("set_floor needs geo, network and floor_change_pct")
        action.update(geo=geo, network=net, change_pct=float(args["floor_change_pct"]))
    elif kind == "disable_network":
        if geo is None or net is None:
            raise ValueError("disable_network needs geo and network")
        action.update(geo=geo, network=net)
    elif kind == "escalate":
        action["team"] = args.get("escalate_to") or "ad-ops on-call"
    else:
        raise ValueError("action_type must be rollback_config, set_floor, disable_network or escalate")
    return {
        "root_cause": {"category": cat, "geo": geo, "network": net,
                       "explanation": str(args.get("explanation", "")).strip()},
        "action": action,
        "confidence": float(args.get("confidence", 0.5)),
    }


class ToolContext:
    """Binds the tools to one world and one incident, and records every call."""

    def __init__(self, world, incident: dict, on_call: Callable[[str, dict, dict], None] | None = None) -> None:
        self.world = world
        self.baseline = world.baseline
        self.incident = incident
        self.on_call = on_call

    # ---- plumbing ----
    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            out = {"error": f"unknown tool {name!r}"}
        else:
            try:
                with self.world.lock:
                    out = fn(**args)
            except (TypeError, ValueError, KeyError) as exc:
                out = {"error": f"{type(exc).__name__}: {exc}"}
        if self.on_call:
            self.on_call(name, args, out)
        return out

    def _window(self) -> tuple[int, int]:
        t1 = self.world.tick
        t0 = max(self.incident.get("onset_tick", t1 - 2), t1 - MAX_ANALYSIS_WINDOW + 1)
        return min(t0, t1 - 2), t1

    @staticmethod
    def _scope(geo: str | None, fmt: str | None) -> list[int]:
        if geo is not None and geo not in GEOS:
            raise ValueError(f"geo must be one of {GEOS}")
        if fmt is not None and fmt not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        return [i for i, (g, f) in enumerate(SLICES) if (geo is None or g == geo) and (fmt is None or f == fmt)]

    # ---- tools ----
    def tool_decompose_revenue(self, geo: str | None = None, format: str | None = None) -> dict:
        idx = self._scope(geo, format)
        t0, t1 = self._window()
        As, _ = self.world.metrics.window(t0, t1)
        Es, _ = self.baseline.window(t0, t1)
        dau = sum(DAU[g] for g in (GEOS if geo is None else [geo]))
        out = rca.decompose(As[idx].sum(axis=0), Es[idx].sum(axis=0), dau)
        out["scope"] = {"geo": geo or "all", "format": format or "all"}
        out["window"] = f"{t1 - t0 + 1} sim-minutes since incident onset vs seasonal baseline"
        return out

    def tool_localize_revenue_change(self) -> dict:
        t0, t1 = self._window()
        _, An = self.world.metrics.window(t0, t1)
        _, En = self.baseline.window(t0, t1)
        a, e = An[:, :, N_REV], En[:, :, N_REV]                  # [NS, NN]
        gi = np.array([GEOS.index(g) for g, _ in SLICES])
        fi = np.array([FORMATS.index(f) for _, f in SLICES])
        by_geo = lambda x: np.array([x[gi == i].sum() for i in range(len(GEOS))])
        by_fmt = lambda x: np.array([x[fi == i].sum() for i in range(len(FORMATS))])
        dims = {
            "network": (NETWORKS, a.sum(axis=0), e.sum(axis=0)),
            "geo": (GEOS, by_geo(a), by_geo(e)),
            "format": (FORMATS, by_fmt(a), by_fmt(e)),
        }
        cells = []
        for g_i, g in enumerate(GEOS):
            for k, n in enumerate(NETWORKS):
                av, ev = float(a[gi == g_i, k].sum()), float(e[gi == g_i, k].sum())
                if ev > 0:
                    cells.append({"network": n, "geo": g, "actual": round(av, 4), "expected": round(ev, 4),
                                  "delta": round(av - ev, 4), "change_pct": round((av / ev - 1) * 100, 1)})
        cells.sort(key=lambda c: c["delta"])
        total_a, total_e = float(a.sum()), float(e.sum())
        return {"total_revenue": {"actual": round(total_a, 4), "expected": round(total_e, 4),
                                  "change_pct": round((total_a / total_e - 1) * 100, 2)},
                "dimensions": rca.adtributor(dims),
                "largest_losses_network_x_geo": cells[:5]}

    def tool_network_health(self, geo: str, format: str | None = None) -> dict:
        idx = self._scope(geo, format)
        t0, t1 = self._window()
        _, An = self.world.metrics.window(t0, t1)
        _, En = self.baseline.window(t0, t1)
        return {"scope": {"geo": geo, "format": format or "all"},
                "networks": rca.network_health(An[idx].sum(axis=0), En[idx].sum(axis=0))}

    def tool_get_config_changes(self, lookback_minutes: int = 60) -> dict:
        opened = self.incident["opened_tick"]
        changes = [v for v in self.world.cfg.changes_between(opened - int(lookback_minutes), self.world.tick)
                   if v["actor"] not in AGENT_ACTORS]
        out = []
        for v in changes:
            groups: dict[tuple, dict] = {}
            for d in v["diff"]:
                geo = d["slice"].split("|")[0]
                key = (d["section"], d["network"], geo)
                g = groups.setdefault(key, {"section": d["section"], "network": d["network"], "geo": geo,
                                            "example_old": d["old"], "example_new": d["new"], "values_changed": 0})
                g["values_changed"] += 1
            out.append({"version": v["version"], "rollback_target": v["version"] - 1,
                        "pushed": sim_time_label(v["tick"]),
                        "minutes_before_detection": opened - v["tick"],
                        "actor": v["actor"], "note": v["note"], "changes": list(groups.values())[:12]})
        return {"live_version": self.world.cfg.version, "recent_changes": out,
                "note": "No config pushes in the window." if not out else
                        "A push shortly before detection is a prime suspect; confirm it touches the affected scope."}

    def tool_query_metrics(self, geo: str | None = None, format: str | None = None,
                           network: str | None = None, last_minutes: int = 30) -> dict:
        idx = self._scope(geo, format)
        if network is not None and network not in NETWORKS:
            raise ValueError(f"network must be one of {NETWORKS}")
        t1 = self.world.tick
        rows = []
        for b1 in range(t1, t1 - int(last_minutes), -5):
            b0 = b1 - 4
            As, An = self.world.metrics.window(b0, b1)
            Es, En = self.baseline.window(b0, b1)
            a, e = As[idx].sum(axis=0), Es[idx].sum(axis=0)
            row = {"bucket_end": sim_time_label(b1),
                   "revenue": [round(float(a[S_REV]), 3), round(float(e[S_REV]), 3)],
                   "fill_rate": [round(float(a[S_FILL] / max(a[S_REQ], 1e-9)), 3),
                                 round(float(e[S_FILL] / max(e[S_REQ], 1e-9)), 3)],
                   "ecpm": [round(float(1000 * a[S_REV] / max(a[S_IMP], 1e-9)), 3),
                            round(float(1000 * e[S_REV] / max(e[S_IMP], 1e-9)), 3)]}
            if network:
                k = NET_INDEX[network]
                na, ne = An[idx, k].sum(axis=0), En[idx, k].sum(axis=0)
                for label, num, den in (("response_rate", N_RESP, N_ATT), ("avg_price_cpm", N_BID, N_RESP),
                                        ("avg_latency_ms", N_LAT, N_ATT)):
                    row[label] = [round(float(na[num] / max(na[den], 1e-9)), 3),
                                  round(float(ne[num] / max(ne[den], 1e-9)), 3)]
            rows.append(row)
        return {"scope": {"geo": geo or "all", "format": format or "all", "network": network},
                "columns": "[actual, expected] per 5-sim-minute bucket, newest first", "buckets": rows}
