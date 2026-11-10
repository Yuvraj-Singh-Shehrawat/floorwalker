import numpy as np

from app.auction import NO_EFFECTS, Effects, run_auction
from app.configstore import compile_config, default_config
from app.domain import (NB, NN, N_ATT, N_IMP, N_RESP, N_REV, N_TO, N_WIN, SLICES, S_FILL, S_IMP, S_REQ,
                        S_REV, build_params, slice_key)

PARAMS = build_params()
N = 4000


def auction(cfg=None, eff=NO_EFFECTS, s=0, seed=1):
    compiled = compile_config(cfg or default_config())
    return run_auction(PARAMS[s], compiled[s], eff, N, np.random.default_rng(seed))


def test_accounting_is_consistent():
    sl, net = auction()
    assert sl[S_REQ] == N
    assert net[:, N_WIN].sum() == sl[S_FILL]          # every fill has exactly one winner
    assert net[:, N_IMP].sum() == sl[S_IMP] <= sl[S_FILL] <= N
    assert np.isclose(net[:, N_REV].sum(), sl[S_REV])
    assert (net[:, N_RESP] <= net[:, N_ATT]).all()


def test_floors_x5_starve_the_waterfall():
    cfg = default_config()
    g, f = SLICES[0]
    for n in cfg["floors"][slice_key(g, f)]:
        cfg["floors"][slice_key(g, f)][n] *= 5
    base_sl, base_net = auction()
    sl, net = auction(cfg)
    assert net[NB:, N_WIN].sum() < 0.25 * base_net[NB:, N_WIN].sum()
    assert sl[S_REV] < base_sl[S_REV]


def test_disabled_network_is_never_called():
    cfg = default_config()
    g, f = SLICES[0]
    cfg["enabled"][slice_key(g, f)]["Network A"] = False
    cfg["enabled"][slice_key(g, f)]["Network D"] = False
    _, net = auction(cfg)
    assert net[0].sum() == 0 and net[3].sum() == 0


def test_tight_timeout_kills_bids():
    cfg = default_config()
    g, f = SLICES[0]
    for n in ("Network A", "Network B", "Network C"):
        cfg["timeout_ms"][slice_key(g, f)][n] = 120
    _, net = auction(cfg)
    assert net[:NB, N_RESP].sum() < 0.1 * N
    assert net[:NB, N_TO].sum() > 2.5 * N


def test_slow_network_costs_show_rate():
    eff = Effects(lat_mult=np.array([8.0] + [1.0] * (NN - 1)))
    base_sl, _ = auction()
    sl, _ = auction(eff=eff)
    assert sl[S_IMP] / sl[S_FILL] < 0.9 * base_sl[S_IMP] / base_sl[S_FILL]
