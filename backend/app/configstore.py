"""Versioned mediation config with diffs, an audit log and rollback.

Config shape (plain JSON so it diffs cleanly):
  floors[slice_key][network]      -> USD CPM floor (waterfall networks only)
  enabled[slice_key][network]     -> bool
  timeout_ms[slice_key][network]  -> int
"""
from __future__ import annotations

import copy

import numpy as np

from .auction import SliceConfig
from .domain import (BIDDERS, DEFAULT_BIDDER_TIMEOUT_MS, DEFAULT_FLOOR_MULT, DEFAULT_WF_TIMEOUT_MS,
                     FORMAT_BASE_ECPM, GEO_ECPM_MULT, NETWORKS, SLICES, WATERFALL, slice_key)


def default_config() -> dict:
    cfg: dict = {"floors": {}, "enabled": {}, "timeout_ms": {}}
    for g, f in SLICES:
        k = slice_key(g, f)
        base = FORMAT_BASE_ECPM[f] * GEO_ECPM_MULT[g]
        cfg["floors"][k] = {n: round(base * DEFAULT_FLOOR_MULT[n], 4) for n in WATERFALL}
        cfg["enabled"][k] = {n: True for n in NETWORKS}
        cfg["timeout_ms"][k] = {n: (DEFAULT_BIDDER_TIMEOUT_MS if n in BIDDERS else DEFAULT_WF_TIMEOUT_MS)
                                for n in NETWORKS}
    return cfg


def compile_config(cfg: dict) -> list[SliceConfig]:
    out = []
    for g, f in SLICES:
        k = slice_key(g, f)
        out.append(SliceConfig(
            floors=np.array([float(cfg["floors"][k].get(n, 0.0)) for n in NETWORKS]),
            enabled=np.array([bool(cfg["enabled"][k][n]) for n in NETWORKS]),
            timeout_ms=np.array([float(cfg["timeout_ms"][k][n]) for n in NETWORKS]),
        ))
    return out


def diff_configs(a: dict, b: dict) -> list[dict]:
    """List of {section, slice, network, old, new} for every value that differs."""
    out = []
    for section in ("floors", "enabled", "timeout_ms"):
        for sk, nets in b[section].items():
            for n, new in nets.items():
                old = a[section].get(sk, {}).get(n)
                if old != new:
                    out.append({"section": section, "slice": sk, "network": n, "old": old, "new": new})
    return out


def changed_slices(a: dict, b: dict) -> set[str]:
    return {d["slice"] for d in diff_configs(a, b)}


class ConfigStore:
    def __init__(self) -> None:
        cfg = default_config()
        self.versions: list[dict] = [{
            "version": 1, "tick": 0, "actor": "system", "note": "Initial mediation config",
            "config": cfg, "diff": [],
        }]
        self._compiled = compile_config(cfg)

    @property
    def version(self) -> int:
        return self.versions[-1]["version"]

    def current(self) -> dict:
        return copy.deepcopy(self.versions[-1]["config"])

    def compiled(self) -> list[SliceConfig]:
        return self._compiled

    def get(self, version: int) -> dict:
        for v in self.versions:
            if v["version"] == version:
                return copy.deepcopy(v["config"])
        raise KeyError(f"config version {version} not found")

    def push(self, new_cfg: dict, actor: str, note: str, tick: int) -> int:
        """Make new_cfg live. Returns the new version number (or the current one if nothing changed)."""
        diff = diff_configs(self.versions[-1]["config"], new_cfg)
        if not diff:
            return self.version
        v = self.version + 1
        self.versions.append({"version": v, "tick": tick, "actor": actor, "note": note,
                              "config": copy.deepcopy(new_cfg), "diff": diff})
        self._compiled = compile_config(new_cfg)
        return v

    def changes_between(self, t0: int, t1: int) -> list[dict]:
        return [v for v in self.versions if t0 <= v["tick"] <= t1 and v["version"] > 1]

    def audit_log(self, limit: int = 30) -> list[dict]:
        return [{k: v[k] for k in ("version", "tick", "actor", "note", "diff")}
                for v in self.versions[-limit:]][::-1]
