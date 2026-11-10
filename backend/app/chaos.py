"""Incident injection ("Break something").

Two kinds of incident:
  * config    - a bad config push by some other actor. It shows up in the audit log,
                exactly like a real bad release would. Fix = roll back.
  * external  - something changed outside our control (a network got slow, demand fell,
                traffic fell). Nothing in the config explains it.

`truth` is the ground-truth label used ONLY by evals. Agent tools never read it.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from .domain import BIDDERS, FORMATS, GEOS, NETWORKS, NET_INDEX, NN, NS, SLICES, WATERFALL, slice_key

CATALOG = {
    "timeout_misconfig": {
        "label": "Bidder timeout misconfigured",
        "kind": "config",
        "description": "A release tightens bidder timeouts so far that bids stop arriving in one geo.",
    },
    "bad_floor_push": {
        "label": "Bad floor push",
        "kind": "config",
        "description": "A pricing update multiplies waterfall floors by 5 in one geo; fill collapses.",
    },
    "network_latency_spike": {
        "label": "Network latency spike",
        "kind": "external",
        "description": "One network gets 8x slower in one geo; auctions drag and show rate falls.",
    },
    "demand_drop": {
        "label": "Demand drop",
        "kind": "external",
        "description": "One network's bids fall by more than half in one geo.",
    },
    "traffic_drop": {
        "label": "Traffic drop",
        "kind": "external",
        "description": "Ad requests from one geo fall by more than half. Not an ad-stack problem.",
    },
}

_ids = itertools.count(1)


@dataclass
class ChaosEvent:
    id: str
    type: str
    kind: str
    geo: str
    network: str | None
    start_tick: int
    end_tick: int
    truth: dict
    active: bool = True
    incident_id: str | None = None
    mults: dict = field(default_factory=dict)   # external only


def _recompute(world) -> None:
    resp, bid, lat, traffic = np.ones((NS, NN)), np.ones((NS, NN)), np.ones((NS, NN)), np.ones(NS)
    for ev in world.chaos_events:
        if not ev.active or ev.kind != "external":
            continue
        for s, (g, _f) in enumerate(SLICES):
            if g != ev.geo:
                continue
            traffic[s] *= ev.mults.get("traffic", 1.0)
            if ev.network:
                k = NET_INDEX[ev.network]
                resp[s, k] *= ev.mults.get("resp", 1.0)
                bid[s, k] *= ev.mults.get("bid", 1.0)
                lat[s, k] *= ev.mults.get("lat", 1.0)
    world.set_effects(resp, bid, lat, traffic)


def inject(world, ctype: str, geo: str | None = None, network: str | None = None,
           duration: int = 240) -> ChaosEvent:
    if ctype not in CATALOG:
        raise ValueError(f"unknown chaos type {ctype!r}")
    rng = world.rng
    with world.lock:
        geo = geo or str(rng.choice(GEOS))
        if geo not in GEOS:
            raise ValueError(f"unknown geo {geo!r}")
        kind = CATALOG[ctype]["kind"]
        t = world.tick
        mults: dict = {}
        if ctype == "timeout_misconfig":
            network = None
            cfg = world.cfg.current()
            for f in FORMATS:
                for n in BIDDERS:
                    cfg["timeout_ms"][slice_key(geo, f)][n] = 120
            world.cfg.push(cfg, actor="release-bot", tick=t,
                           note="Ad-load latency work: tighten bidder timeouts")
            category = "config_change"
        elif ctype == "bad_floor_push":
            network = None
            cfg = world.cfg.current()
            for f in FORMATS:
                for n in WATERFALL:
                    k = slice_key(geo, f)
                    cfg["floors"][k][n] = round(cfg["floors"][k][n] * 5, 4)
            world.cfg.push(cfg, actor="pricing-sync", tick=t,
                           note="Weekly floor refresh from pricing sheet")
            category = "config_change"
        elif ctype == "network_latency_spike":
            network = network if network in NETWORKS else str(rng.choice(NETWORKS))
            mults = {"lat": 8.0}
            category = "network_latency"
        elif ctype == "demand_drop":
            network = network if network in NETWORKS else str(rng.choice(NETWORKS))
            mults = {"bid": 0.4}
            category = "demand_drop"
        else:  # traffic_drop
            network = None
            mults = {"traffic": 0.45}
            category = "traffic_drop"

        ev = ChaosEvent(id=f"chaos-{next(_ids)}", type=ctype, kind=kind, geo=geo, network=network,
                        start_tick=t, end_tick=t + duration, mults=mults,
                        truth={"category": category, "geo": geo, "network": network})
        world.chaos_events.append(ev)
        _recompute(world)
        return ev


def active_event(world) -> ChaosEvent | None:
    for ev in reversed(world.chaos_events):
        if ev.active:
            return ev
    return None


def clear(world) -> None:
    """End every active chaos event (external effects stop; config is NOT touched)."""
    with world.lock:
        for ev in world.chaos_events:
            ev.active = False
        _recompute(world)
