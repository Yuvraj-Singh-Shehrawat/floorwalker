import math

import numpy as np

from app import rca
from app.domain import SK, S_FILL, S_IMP, S_REQ, S_REV


def vec(req, fill, imp, rev):
    v = np.zeros(SK)
    v[S_REQ], v[S_FILL], v[S_IMP], v[S_REV] = req, fill, imp, rev
    return v


def test_decomposition_is_exact():
    exp, act = vec(1000, 950, 800, 4.0), vec(900, 700, 560, 3.1)
    out = rca.decompose(act, exp, dau=5000)
    assert math.isclose(sum(c["dlog"] for c in out["components"]), math.log(3.1 / 4.0), abs_tol=1e-4)
    assert math.isclose(sum(c["share_of_change_pct"] for c in out["components"]), 100, abs_tol=0.5)


def test_traffic_drop_is_attributed_to_requests():
    out = rca.decompose(vec(500, 475, 400, 2.0), vec(1000, 950, 800, 4.0), dau=5000)
    assert out["dominant_component"] == "requests_per_dau"
    req = out["components"][0]
    assert req["share_of_change_pct"] > 99 and math.isclose(req["change_pct"], -50, abs_tol=0.1)


def test_fill_drop_is_attributed_to_fill():
    out = rca.decompose(vec(1000, 475, 400, 2.0), vec(1000, 950, 800, 4.0), dau=5000)
    assert out["dominant_component"] == "fill_rate"


def test_adtributor_finds_the_culprit():
    expected = np.array([10.0, 10.0, 10.0, 10.0])
    actual = np.array([10.0, 2.0, 10.0, 10.0])
    flat = np.array([7.9, 8.0, 8.1, 8.0])   # same total drop, spread evenly
    res = rca.adtributor({"network": (list("ABCD"), actual, expected), "geo": (list("WXYZ"), flat, expected)})
    assert res[0]["dimension"] == "network" and res[0]["explains"]
    assert [e["name"] for e in res[0]["elements"]] == ["B"]
    assert res[0]["surprise"] > 10 * res[1]["surprise"]
