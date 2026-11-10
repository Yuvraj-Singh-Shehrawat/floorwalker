"""Hybrid mediation auction, vectorised over n ad requests of one slice.

Flow for each request (this is how hybrid mediation works in most mobile games):
  1. All enabled in-app bidders are called in parallel. Each may bid (first-price)
     or time out. The highest valid bid is the "bidding winner".
  2. Waterfall lines are walked from highest floor to lowest. A line is only
     attempted if its floor is ABOVE the best bid (otherwise the bid already beats it).
     A line fills if it has an ad and its valuation clears its floor; it pays
     max(floor, 0.8 * valuation).
  3. If no waterfall line filled, the bidding winner wins at its bid.
  4. Latency accumulates (bidding stage + every waterfall attempt). Slow auctions
     lose impressions: show probability decays with total latency.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .domain import (NB, NK, NN, N_ATT, N_BID, N_IMP, N_LAT, N_RESP, N_REV, N_TO, N_WIN,
                     SHOW_TAU_MS, SK, S_FILL, S_IMP, S_LAT, S_REQ, S_REV, LAT_SIGMA, SliceParams)


@dataclass
class SliceConfig:
    floors: np.ndarray      # [NN] USD CPM; 0 for bidders
    enabled: np.ndarray     # [NN] bool
    timeout_ms: np.ndarray  # [NN]


@dataclass
class Effects:
    """Multipliers applied by chaos incidents. All ones = healthy."""
    resp_mult: np.ndarray = field(default_factory=lambda: np.ones(NN))
    bid_mult: np.ndarray = field(default_factory=lambda: np.ones(NN))
    lat_mult: np.ndarray = field(default_factory=lambda: np.ones(NN))


NO_EFFECTS = Effects()


def run_auction(p: SliceParams, c: SliceConfig, e: Effects, n: int,
                rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Simulate n requests. Returns (slice_stats[SK], net_stats[NN, NK]) as raw sample counts."""
    net = np.zeros((NN, NK))

    # ---- 1. in-app bidding (first-price) ----
    en_b = c.enabled[:NB]
    lat_b = rng.lognormal(np.log(p.lat_median[:NB] * e.lat_mult[:NB]), LAT_SIGMA, (n, NB))
    bids = rng.lognormal(np.log(p.median[:NB] * e.bid_mult[:NB]), p.sigma[:NB], (n, NB))
    wants = rng.random((n, NB)) < np.clip(p.p_resp[:NB] * e.resp_mult[:NB], 0.0, 1.0)
    timed_out = (lat_b > c.timeout_ms[:NB]) & en_b
    valid = wants & ~timed_out & en_b
    bid_vals = np.where(valid, bids, 0.0)
    best_bid = bid_vals.max(axis=1)
    best_idx = bid_vals.argmax(axis=1)
    has_bid = best_bid > 0.0
    waited = np.where(en_b, np.minimum(lat_b, c.timeout_ms[:NB]), 0.0)
    total_lat = waited.max(axis=1)

    net[:NB, N_ATT] = np.where(en_b, n, 0)
    net[:NB, N_RESP] = valid.sum(axis=0)
    net[:NB, N_LAT] = waited.sum(axis=0)
    net[:NB, N_TO] = timed_out.sum(axis=0)
    net[:NB, N_BID] = bid_vals.sum(axis=0)

    # ---- 2. waterfall, highest floor first ----
    filled = np.zeros(n, dtype=bool)
    winner = np.full(n, -1)
    price = np.zeros(n)
    for j in np.argsort(-c.floors[NB:], kind="stable"):
        k = NB + int(j)
        if not c.enabled[k]:
            continue
        floor = c.floors[k]
        attempted = ~filled & (floor > best_bid)
        n_att = int(attempted.sum())
        if n_att == 0:
            continue
        lat_k = rng.lognormal(np.log(p.lat_median[k] * e.lat_mult[k]), LAT_SIGMA, n)
        to_k = lat_k > c.timeout_ms[k]
        v = rng.lognormal(np.log(p.median[k] * e.bid_mult[k]), p.sigma[k], n)
        has_ad = rng.random(n) < min(1.0, p.p_resp[k] * e.resp_mult[k])
        fill = attempted & has_ad & ~to_k & (v >= floor)
        wait_k = np.where(attempted, np.minimum(lat_k, c.timeout_ms[k]), 0.0)
        total_lat = total_lat + wait_k
        pay = np.maximum(floor, 0.8 * v)
        winner[fill] = k
        price[fill] = pay[fill]
        filled |= fill
        net[k, N_ATT] = n_att
        net[k, N_RESP] = fill.sum()
        net[k, N_LAT] = wait_k.sum()
        net[k, N_TO] = (attempted & to_k).sum()
        net[k, N_BID] = pay[fill].sum()

    # ---- 3. bidding winner takes what the waterfall did not ----
    bid_wins = ~filled & has_bid
    winner[bid_wins] = best_idx[bid_wins]
    price[bid_wins] = best_bid[bid_wins]
    filled |= bid_wins

    # ---- 4. show rate depends on how long the user waited ----
    p_show = p.base_show * np.exp(-total_lat / SHOW_TAU_MS)
    shown = filled & (rng.random(n) < p_show)

    net[:, N_WIN] = np.bincount(winner[filled], minlength=NN)
    net[:, N_IMP] = np.bincount(winner[shown], minlength=NN)
    net[:, N_REV] = np.bincount(winner[shown], weights=price[shown], minlength=NN) / 1000.0

    sl = np.zeros(SK)
    sl[S_REQ] = n
    sl[S_FILL] = filled.sum()
    sl[S_IMP] = shown.sum()
    sl[S_REV] = price[shown].sum() / 1000.0
    sl[S_LAT] = total_lat.sum()
    return sl, net
