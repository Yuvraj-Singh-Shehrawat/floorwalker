"""Domain constants for the simulated ad stack.

Everything here is SIMULATED. Network names are fictional on purpose.
Change numbers here to reshape the world; nothing else hardcodes them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

GEOS = ["IN", "US", "BR", "ID", "DE"]
FORMATS = ["rewarded", "interstitial", "banner"]
BIDDERS = ["Network A", "Network B", "Network C"]      # in-app bidding, first-price
WATERFALL = ["Network D", "Network E", "Network F"]    # floored waterfall lines
NETWORKS = BIDDERS + WATERFALL
NB = len(BIDDERS)
NN = len(NETWORKS)

SLICES = [(g, f) for g in GEOS for f in FORMATS]
NS = len(SLICES)
SLICE_INDEX = {s: i for i, s in enumerate(SLICES)}
NET_INDEX = {n: i for i, n in enumerate(NETWORKS)}


def slice_key(geo: str, fmt: str) -> str:
    return f"{geo}|{fmt}"


SLICE_KEYS = [slice_key(g, f) for g, f in SLICES]

DAU = {"IN": 40_000, "US": 12_000, "BR": 15_000, "ID": 18_000, "DE": 6_000}
TOTAL_DAU = sum(DAU.values())
TZ_OFFSET_H = {"IN": 5.5, "US": -5.0, "BR": -3.0, "ID": 7.0, "DE": 1.0}
GEO_ECPM_MULT = {"US": 1.0, "DE": 0.7, "BR": 0.2, "ID": 0.14, "IN": 0.11}
FORMAT_BASE_ECPM = {"rewarded": 16.0, "interstitial": 9.0, "banner": 0.5}   # USD, US baseline
REQ_PER_DAU_DAY = {"rewarded": 2.5, "interstitial": 4.0, "banner": 18.0}
BASE_SHOW = {"rewarded": 0.97, "interstitial": 0.92, "banner": 0.99}
SHOW_TAU_MS = 2500.0        # show rate decays as exp(-auction_latency / tau)
MINUTES_PER_DAY = 1440

DEFAULT_BIDDER_TIMEOUT_MS = 1000
DEFAULT_WF_TIMEOUT_MS = 1500

# Columns of the per-slice metric array
S_REQ, S_FILL, S_IMP, S_REV, S_LAT = range(5)
SK = 5
# Columns of the per-network metric array
N_ATT, N_RESP, N_WIN, N_IMP, N_REV, N_LAT, N_TO, N_BID = range(8)
NK = 8

# Median bid / valuation as a multiple of the slice's base eCPM
_STRENGTH = {"Network A": 1.00, "Network B": 0.85, "Network C": 0.92,
             "Network D": 1.50, "Network E": 0.90, "Network F": 0.45}
_SIGMA = {"Network A": 0.60, "Network B": 0.60, "Network C": 0.60,
          "Network D": 0.70, "Network E": 0.70, "Network F": 0.70}
# Bidders: probability of bidding. Waterfall: probability the line has an ad at all.
_P_RESP = {"Network A": 0.50, "Network B": 0.40, "Network C": 0.45,
           "Network D": 0.90, "Network E": 0.80, "Network F": 0.70}
_LAT_MEDIAN = {"Network A": 240.0, "Network B": 260.0, "Network C": 250.0,
               "Network D": 320.0, "Network E": 300.0, "Network F": 280.0}
LAT_SIGMA = 0.35
# Default waterfall floors as a multiple of the slice's base eCPM
DEFAULT_FLOOR_MULT = {"Network D": 1.60, "Network E": 0.80, "Network F": 0.30}


@dataclass(frozen=True)
class SliceParams:
    base_ecpm: float
    median: np.ndarray      # [NN] bid / valuation median, USD CPM
    sigma: np.ndarray       # [NN]
    p_resp: np.ndarray      # [NN]
    lat_median: np.ndarray  # [NN] ms
    base_show: float


def build_params() -> list[SliceParams]:
    """Per-slice demand parameters. Deterministic: same world every run."""
    rng = np.random.default_rng(7)
    affinity = rng.uniform(0.8, 1.2, size=(len(GEOS), NN))  # each network is stronger in some geos
    out = []
    for g, f in SLICES:
        base = FORMAT_BASE_ECPM[f] * GEO_ECPM_MULT[g]
        gi = GEOS.index(g)
        out.append(SliceParams(
            base_ecpm=base,
            median=np.array([base * _STRENGTH[n] * affinity[gi, i] for i, n in enumerate(NETWORKS)]),
            sigma=np.array([_SIGMA[n] for n in NETWORKS]),
            p_resp=np.array([_P_RESP[n] for n in NETWORKS]),
            lat_median=np.array([_LAT_MEDIAN[n] for n in NETWORKS]),
            base_show=BASE_SHOW[f],
        ))
    return out


def diurnal(tick: int, geo: str) -> float:
    """Traffic multiplier by local time of day; peaks at 20:00 local."""
    local_h = ((tick % MINUTES_PER_DAY) / 60.0 + TZ_OFFSET_H[geo]) % 24.0
    return 1.0 + 0.45 * math.sin(2.0 * math.pi * (local_h - 14.0) / 24.0)


def expected_requests(tick: int, geo: str, fmt: str) -> float:
    """Ad requests per sim-minute for a slice, before noise and chaos."""
    return DAU[geo] * REQ_PER_DAU_DAY[fmt] / MINUTES_PER_DAY * diurnal(tick, geo)


def sim_time_label(tick: int) -> str:
    day = tick // MINUTES_PER_DAY
    m = tick % MINUTES_PER_DAY
    return f"Day {day} {m // 60:02d}:{m % 60:02d} UTC"
