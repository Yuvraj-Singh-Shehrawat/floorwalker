"""The simulated world: a clock, traffic, the auction, metrics, and canary traffic splitting.

One call to World.step() advances one sim-minute. The API server calls it once per
real second (60x); evals call it in a tight loop. Nothing here knows about wall-clock time.

Sampling note: each slice simulates `sample_n` requests per sim-minute and scales the
result to that slice's real traffic volume. This keeps every slice equally observable.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

import numpy as np

from .auction import Effects, SliceConfig, run_auction
from .configstore import ConfigStore, changed_slices, compile_config
from .domain import (MINUTES_PER_DAY, NK, NN, NS, SK, SLICE_KEYS, SLICES, S_REQ, S_REV,
                     build_params, expected_requests)
from .metrics import Baseline, MetricStore


@dataclass
class Canary:
    """A candidate config served to `fraction` of traffic on the slices it changes."""
    incident_id: str
    config: dict
    compiled: list[SliceConfig]
    scope: set[int]                 # slice indices whose config differs from live
    fraction: float
    start_tick: int
    control: list[float] = field(default_factory=lambda: [0.0, 0.0])   # [requests, revenue]
    treat: list[float] = field(default_factory=lambda: [0.0, 0.0])

    def rpr(self) -> tuple[float, float]:
        """Revenue per request: (control, canary)."""
        c = self.control[1] / self.control[0] if self.control[0] else 0.0
        t = self.treat[1] / self.treat[0] if self.treat[0] else 0.0
        return c, t


class World:
    def __init__(self, seed: int = 0, sample_n: int = 300, start_tick: int = -1,
                 baseline: Baseline | None = None) -> None:
        self.params = build_params()
        self.rng = np.random.default_rng(seed)
        self.sample_n = sample_n
        self.tick = start_tick                 # last completed sim-minute
        self.cfg = ConfigStore()
        self.metrics = MetricStore()
        self.baseline = baseline
        self.canary: Canary | None = None
        self.chaos_events: list = []           # see chaos.py
        self.lock = threading.RLock()
        self._resp = np.ones((NS, NN))
        self._bid = np.ones((NS, NN))
        self._lat = np.ones((NS, NN))
        self._traffic = np.ones(NS)

    # ---- chaos multipliers (set by chaos.py) ----
    def set_effects(self, resp: np.ndarray, bid: np.ndarray, lat: np.ndarray, traffic: np.ndarray) -> None:
        with self.lock:
            self._resp, self._bid, self._lat, self._traffic = resp, bid, lat, traffic

    # ---- canary ----
    def start_canary(self, incident_id: str, candidate: dict, fraction: float) -> Canary:
        with self.lock:
            scope_keys = changed_slices(self.cfg.current(), candidate)
            self.canary = Canary(
                incident_id=incident_id, config=copy.deepcopy(candidate),
                compiled=compile_config(candidate),
                scope={SLICE_KEYS.index(k) for k in scope_keys},
                fraction=fraction, start_tick=self.tick + 1,
            )
            return self.canary

    def end_canary(self) -> Canary | None:
        with self.lock:
            c, self.canary = self.canary, None
            return c

    # ---- the clock ----
    def step(self) -> int:
        with self.lock:
            self.tick += 1
            t = self.tick
            live = self.cfg.compiled()
            sl_all = np.zeros((NS, SK))
            net_all = np.zeros((NS, NN, NK))
            n = self.sample_n
            can = self.canary
            for s, (g, f) in enumerate(SLICES):
                reqs = expected_requests(t, g, f) * self._traffic[s] * max(0.0, 1.0 + 0.02 * self.rng.standard_normal())
                eff = Effects(self._resp[s], self._bid[s], self._lat[s])
                sl, net = run_auction(self.params[s], live[s], eff, n, self.rng)
                w = reqs / n
                if can is not None and s in can.scope:
                    fr = can.fraction
                    sl2, net2 = run_auction(self.params[s], can.compiled[s], eff, n, self.rng)
                    can.control[0] += reqs * (1 - fr)
                    can.control[1] += sl[S_REV] * w * (1 - fr)
                    can.treat[0] += reqs * fr
                    can.treat[1] += sl2[S_REV] * w * fr
                    sl_all[s] = sl * w * (1 - fr) + sl2 * w * fr
                    net_all[s] = net * w * (1 - fr) + net2 * w * fr
                else:
                    sl_all[s] = sl * w
                    net_all[s] = net * w
            self.metrics.add(t, sl_all, net_all)
            return t

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.step()


def _cache_key(seed: int, sample_n: int, sigma_ticks: int) -> str:
    """Changes whenever the world's code or parameters change, so a stale cache is never used."""
    import hashlib
    from pathlib import Path
    h = hashlib.sha256(f"{seed}|{sample_n}|{sigma_ticks}".encode())
    here = Path(__file__).resolve().parent
    for name in ("domain.py", "auction.py", "configstore.py", "simulator.py", "metrics.py", "detection.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()[:16]


def bootstrap_world(seed: int = 0, sample_n: int = 300, sigma_ticks: int = 480,
                    cache_dir: str | None = None, history_ticks: int = 180) -> World:
    """Build a world with a fitted baseline.

    Runs one clean sim-day (becomes the seasonal baseline) plus `sigma_ticks` more clean
    minutes (calibrates noise per series). Takes ~10 s; pass cache_dir to make restarts instant.
    """
    import os
    from .detection import ALL_SERIES, fit_sigma   # local import avoids a cycle

    end = MINUTES_PER_DAY + sigma_ticks
    path = os.path.join(cache_dir, f"baseline_{_cache_key(seed, sample_n, sigma_ticks)}.npz") if cache_dir else None
    if path and os.path.exists(path):
        z = np.load(path)
        base = Baseline(z["slice"].astype(float), z["net"].astype(float))
        base.sigma = {k: z[f"sigma_{k}"].astype(float) for k in ALL_SERIES}
        w = World(seed=seed + 1, sample_n=sample_n, start_tick=end - history_ticks - 1, baseline=base)
        w.run(history_ticks)      # refill recent history so charts are not empty
        return w

    w = World(seed=seed, sample_n=sample_n)
    w.run(end)
    w.baseline = Baseline.fit(w.metrics, day_start_tick=0)
    w.baseline.sigma = fit_sigma(w.metrics, w.baseline, MINUTES_PER_DAY, end - 1)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        np.savez_compressed(path, slice=w.baseline.slice.astype(np.float32), net=w.baseline.net.astype(np.float32),
                            **{f"sigma_{k}": v.astype(np.float32) for k, v in w.baseline.sigma.items()})
    return w
