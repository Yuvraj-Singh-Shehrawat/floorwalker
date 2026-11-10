"""Deterministic root-cause tools. Pure functions on arrays; no LLM, no I/O.

1. decompose()  - exact log decomposition of revenue per DAU:

       rev/DAU = (requests/DAU) x fill x show x (eCPM/1000)
       dlog(rev/DAU) = dlog(req/DAU) + dlog(fill) + dlog(show) + dlog(eCPM)

   Every revenue drop lands in these four buckets, and the shares add to 100%.

2. adtributor() - which dimension (network / geo / format) and which elements explain a
   revenue change. After Bhagwan et al., "Adtributor: Revenue Debugging in Advertising
   Systems", NSDI 2014: rank elements by "surprise" (Jensen-Shannon divergence between
   expected and actual share), keep those with meaningful "explanatory power".

3. network_health() - per-network vital signs versus expectation for one scope.
"""
from __future__ import annotations

import math

import numpy as np

from .domain import (NETWORKS, N_ATT, N_BID, N_LAT, N_RESP, N_REV, N_TO, S_FILL, S_IMP, S_REQ, S_REV)

EPS = 1e-12
COMPONENTS = ("requests_per_dau", "fill_rate", "show_rate", "ecpm")


def _components(s: np.ndarray, dau: float) -> dict[str, float]:
    return {
        "requests_per_dau": s[S_REQ] / dau,
        "fill_rate": s[S_FILL] / max(s[S_REQ], EPS),
        "show_rate": s[S_IMP] / max(s[S_FILL], EPS),
        "ecpm": 1000.0 * s[S_REV] / max(s[S_IMP], EPS),
    }


def decompose(actual: np.ndarray, expected: np.ndarray, dau: float) -> dict:
    """actual/expected: summed slice-metric vectors [SK] for the scope and window."""
    a, e = _components(actual, dau), _components(expected, dau)
    dlog = {k: math.log(max(a[k], EPS) / max(e[k], EPS)) for k in COMPONENTS}
    total = sum(dlog.values())
    rows = []
    for k in COMPONENTS:
        rows.append({
            "component": k,
            "actual": round(a[k], 5), "expected": round(e[k], 5),
            "change_pct": round((math.exp(dlog[k]) - 1) * 100, 2),
            "dlog": round(dlog[k], 5),
            "share_of_change_pct": round(100 * dlog[k] / total, 1) if abs(total) > 1e-6 else 0.0,
        })
    dominant = min(rows, key=lambda r: r["dlog"]) if total < 0 else max(rows, key=lambda r: r["dlog"])
    return {
        "revenue_per_dau_change_pct": round((math.exp(total) - 1) * 100, 2),
        "dlog_total": round(total, 5),
        "components": rows,
        "dominant_component": dominant["component"],
    }


def _js_term(p: float, q: float) -> float:
    out = 0.0
    if p > 0:
        out += 0.5 * p * math.log(2 * p / (p + q))
    if q > 0:
        out += 0.5 * q * math.log(2 * q / (p + q))
    return out


def adtributor(dimensions: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
               t_eep: float = 0.10, t_ep: float = 0.67) -> list[dict]:
    """dimensions: name -> (labels, actual[n], expected[n]) of an additive measure (revenue).

    Returns one result per dimension, most surprising first. `explains` is True when the
    selected elements account for at least t_ep of the total change.
    """
    results = []
    for dim, (labels, A, F) in dimensions.items():
        A, F = np.asarray(A, float), np.asarray(F, float)
        dA, tA, tF = float(A.sum() - F.sum()), float(A.sum()), float(F.sum())
        if abs(dA) < EPS or tA <= 0 or tF <= 0:
            results.append({"dimension": dim, "elements": [], "explanatory_power": 0.0,
                            "surprise": 0.0, "explains": False})
            continue
        rows = []
        for i, lab in enumerate(labels):
            rows.append({"name": lab, "actual": round(float(A[i]), 4), "expected": round(float(F[i]), 4),
                         "ep": float((A[i] - F[i]) / dA), "surprise": _js_term(F[i] / tF, A[i] / tA)})
        rows.sort(key=lambda r: -r["surprise"])
        chosen, ep, surprise = [], 0.0, 0.0
        for r in rows:
            if r["ep"] > t_eep:
                chosen.append(r)
                ep += r["ep"]
                surprise += r["surprise"]
                if ep > t_ep:
                    break
        for r in chosen:
            r["ep"] = round(r["ep"], 3)
            r["surprise"] = round(r["surprise"], 6)
        results.append({"dimension": dim, "elements": chosen, "explanatory_power": round(ep, 3),
                        "surprise": round(surprise, 6), "explains": ep > t_ep})
    results.sort(key=lambda r: (not r["explains"], -r["surprise"]))
    return results


def network_health(actual: np.ndarray, expected: np.ndarray) -> list[dict]:
    """actual/expected: network-metric arrays [NN, NK] summed over the scope and window."""
    def rate(x, num, den):
        return x[:, num] / np.maximum(x[:, den], EPS)

    a_rev, e_rev = actual[:, N_REV], expected[:, N_REV]
    rows = []
    for k, name in enumerate(NETWORKS):
        if expected[k, N_ATT] <= 0 and actual[k, N_ATT] <= 0:
            continue
        row = {"network": name, "type": "bidder" if k < 3 else "waterfall"}
        for label, num, den, scale in (("response_rate", N_RESP, N_ATT, 1.0), ("avg_price_cpm", N_BID, N_RESP, 1.0),
                                       ("avg_latency_ms", N_LAT, N_ATT, 1.0), ("timeout_rate", N_TO, N_ATT, 1.0)):
            a, e = float(rate(actual, num, den)[k]) * scale, float(rate(expected, num, den)[k]) * scale
            row[label] = {"actual": round(a, 4), "expected": round(e, 4),
                          "ratio": round(a / e, 3) if e > EPS else None}
        row["revenue"] = {"actual": round(float(a_rev[k]), 4), "expected": round(float(e_rev[k]), 4),
                          "ratio": round(float(a_rev[k] / e_rev[k]), 3) if e_rev[k] > EPS else None}
        row["expected_revenue_share"] = round(float(e_rev[k] / max(e_rev.sum(), EPS)), 3)
        rows.append(row)
    return rows
