"""Deterministic anomaly detection: seasonal baseline + robust z-scores.

No LLM here. For each monitored series we compare the last `window` sim-minutes with
what the baseline expects for the same minutes of the day:

    residual = actual / expected - 1
    z        = residual / sigma        (sigma = 1.4826 * MAD of residuals on a clean day)

A series "breaches" when z < -Z and the drop is at least `min_effect`. An incident opens
when any series has breached for `consecutive` sim-minutes in a row.

Monitored series
  per geo x format:            rev, req, fill, show, ecpm
  per network x geo x format:  resp_rate (bid rate / fill rate), avg_bid
"""
from __future__ import annotations

import numpy as np

from .domain import (NETWORKS, N_ATT, N_BID, N_RESP, SLICES, S_FILL, S_IMP, S_REQ, S_REV)
from .metrics import Baseline, MetricStore

SLICE_SERIES = ("rev", "req", "fill", "show", "ecpm")
NET_SERIES = ("resp_rate", "avg_bid")
ALL_SERIES = SLICE_SERIES + NET_SERIES


def _ratio(a_num, a_den, e_num, e_den) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (a_num / a_den) / (e_num / e_den) - 1.0


def residuals(As: np.ndarray, An: np.ndarray, Es: np.ndarray, En: np.ndarray) -> dict[str, np.ndarray]:
    """Relative deviation from expectation for every monitored series. NaN = not measurable."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = {
            "rev": As[:, S_REV] / Es[:, S_REV] - 1.0,
            "req": As[:, S_REQ] / Es[:, S_REQ] - 1.0,
        }
    out["fill"] = _ratio(As[:, S_FILL], As[:, S_REQ], Es[:, S_FILL], Es[:, S_REQ])
    out["show"] = _ratio(As[:, S_IMP], As[:, S_FILL], Es[:, S_IMP], Es[:, S_FILL])
    out["ecpm"] = _ratio(As[:, S_REV], As[:, S_IMP], Es[:, S_REV], Es[:, S_IMP])
    out["resp_rate"] = _ratio(An[:, :, N_RESP], An[:, :, N_ATT], En[:, :, N_RESP], En[:, :, N_ATT])
    out["avg_bid"] = _ratio(An[:, :, N_BID], An[:, :, N_RESP], En[:, :, N_BID], En[:, :, N_RESP])
    return out


def fit_sigma(store: MetricStore, baseline: Baseline, t0: int, t1: int, window: int = 5) -> dict[str, np.ndarray]:
    """Robust noise level per series, from residuals on clean history t0..t1."""
    acc: dict[str, list[np.ndarray]] = {k: [] for k in ALL_SERIES}
    for t in range(t0 + window - 1, t1 + 1):
        r = residuals(*store.window(t - window + 1, t), *baseline.window(t - window + 1, t))
        for k in ALL_SERIES:
            acc[k].append(r[k])
    sigma = {}
    for k, rows in acc.items():
        x = np.stack(rows)
        x[~np.isfinite(x)] = np.nan
        with np.errstate(all="ignore"):
            med = np.nanmedian(x, axis=0)
            mad = np.nanmedian(np.abs(x - med), axis=0)
        sigma[k] = np.maximum(1.4826 * mad, 0.01)
    return sigma


class Detector:
    def __init__(self, baseline: Baseline, z_threshold: float = 5.0, min_effect: float = 0.10,
                 consecutive: int = 3, window: int = 5, max_sigma: float = 0.30) -> None:
        if not baseline.sigma:
            raise ValueError("baseline.sigma is empty; call fit_sigma first")
        self.baseline = baseline
        self.z_threshold = z_threshold
        self.min_effect = min_effect
        self.consecutive = consecutive
        self.window = window
        self.max_sigma = max_sigma
        self.counters = {k: np.zeros_like(baseline.sigma[k], dtype=int) for k in ALL_SERIES}
        self.muted_until = -1

    def mute(self, until_tick: int) -> None:
        self.muted_until = max(self.muted_until, until_tick)
        for c in self.counters.values():
            c[:] = 0

    def zscores(self, store: MetricStore, tick: int) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        t0 = tick - self.window + 1
        r = residuals(*store.window(t0, tick), *self.baseline.window(t0, tick))
        z = {}
        for k in ALL_SERIES:
            sig = self.baseline.sigma[k]
            zk = r[k] / sig
            zk[~np.isfinite(zk) | ~np.isfinite(sig) | (sig > self.max_sigma)] = np.nan
            z[k] = zk
        return z, r

    def check(self, store: MetricStore, tick: int) -> list[dict] | None:
        """Call once per sim-minute. Returns the breaching signals when an incident should open."""
        if tick <= self.muted_until or store.last_tick - (store.first_tick or 0) < self.window:
            return None
        z, r = self.zscores(store, tick)
        fire = False
        for k in ALL_SERIES:
            with np.errstate(invalid="ignore"):
                breach = (z[k] < -self.z_threshold) & (r[k] < -self.min_effect)
            self.counters[k] = np.where(breach, self.counters[k] + 1, 0)
            fire = fire or bool((self.counters[k] >= self.consecutive).any())
        return self.signals(z, r) if fire else None

    def signals(self, z: dict[str, np.ndarray], r: dict[str, np.ndarray], limit: int = 12) -> list[dict]:
        """Every series currently breaching, most anomalous first."""
        out = []
        for k in ALL_SERIES:
            for idx in np.argwhere(self.counters[k] >= 1):
                s = int(idx[0])
                g, f = SLICES[s]
                net = NETWORKS[int(idx[1])] if k in NET_SERIES else None
                key = tuple(idx)
                out.append({"series": k, "geo": g, "format": f, "network": net,
                            "z": round(float(z[k][key]), 1), "change_pct": round(float(r[k][key]) * 100, 1)})
        out.sort(key=lambda d: d["z"])
        return out[:limit]
