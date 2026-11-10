"""Per-sim-minute metric storage (ring buffer) and the seasonal baseline learned from history."""
from __future__ import annotations

import numpy as np

from .domain import MINUTES_PER_DAY, NK, NN, NS, SK


class MetricStore:
    """Ring buffer of the last `capacity` sim-minutes, indexed by absolute tick."""

    def __init__(self, capacity: int = 3 * MINUTES_PER_DAY) -> None:
        self.capacity = capacity
        self.slice = np.zeros((capacity, NS, SK))
        self.net = np.zeros((capacity, NS, NN, NK))
        self.first_tick: int | None = None
        self.last_tick: int = -1

    def add(self, tick: int, sl: np.ndarray, net: np.ndarray) -> None:
        i = tick % self.capacity
        self.slice[i] = sl
        self.net[i] = net
        if self.first_tick is None:
            self.first_tick = tick
        self.last_tick = tick
        if tick - self.first_tick >= self.capacity:
            self.first_tick = tick - self.capacity + 1

    def _idx(self, t0: int, t1: int) -> np.ndarray:
        t0 = max(t0, self.first_tick or 0)
        t1 = min(t1, self.last_tick)
        return np.arange(t0, t1 + 1) % self.capacity

    def stack(self, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray]:
        """Arrays [T, NS, SK] and [T, NS, NN, NK] for ticks t0..t1 inclusive."""
        i = self._idx(t0, t1)
        return self.slice[i], self.net[i]

    def window(self, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray]:
        """Sums over ticks t0..t1 inclusive: [NS, SK] and [NS, NN, NK]."""
        s, n = self.stack(t0, t1)
        return s.sum(axis=0), n.sum(axis=0)


def _circular_smooth(x: np.ndarray, w: int) -> np.ndarray:
    pad = w // 2
    xp = np.concatenate([x[-pad:], x, x[:pad]], axis=0)
    c = np.cumsum(xp, axis=0)
    c = np.concatenate([np.zeros_like(c[:1]), c], axis=0)
    return (c[w:] - c[:-w]) / w


class Baseline:
    """Expected metrics by minute-of-day, learned from one full day of history.

    This is the "same time yesterday" baseline that ad-ops teams use, smoothed
    over +/-15 minutes so one noisy minute does not become the expectation.
    """

    def __init__(self, slice_profile: np.ndarray, net_profile: np.ndarray) -> None:
        self.slice = slice_profile   # [1440, NS, SK]
        self.net = net_profile       # [1440, NS, NN, NK]
        self.sigma: dict[str, np.ndarray] = {}

    @classmethod
    def fit(cls, store: MetricStore, day_start_tick: int, smooth: int = 31) -> "Baseline":
        s, n = store.stack(day_start_tick, day_start_tick + MINUTES_PER_DAY - 1)
        if len(s) != MINUTES_PER_DAY:
            raise ValueError("need one full day of history to fit the baseline")
        shift = day_start_tick % MINUTES_PER_DAY
        s = np.roll(s, shift, axis=0)   # row i == minute-of-day i
        n = np.roll(n, shift, axis=0)
        return cls(_circular_smooth(s, smooth), _circular_smooth(n, smooth))

    def stack(self, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray]:
        i = np.arange(t0, t1 + 1) % MINUTES_PER_DAY
        return self.slice[i], self.net[i]

    def window(self, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray]:
        s, n = self.stack(t0, t1)
        return s.sum(axis=0), n.sum(axis=0)

    def daily_revenue(self) -> float:
        from .domain import S_REV
        return float(self.slice[:, :, S_REV].sum())
