import copy

from app.configstore import ConfigStore, diff_configs
from app.policy import Policy, build_candidate


def setup():
    return Policy.load(), ConfigStore()


def bad_push(cfg: ConfigStore, tick=100) -> int:
    new = cfg.current()
    new["floors"]["US|rewarded"]["Network D"] *= 5
    return cfg.push(new, actor="pricing-sync", note="bad", tick=tick)


def test_floor_step_limit():
    pol, cfg = setup()
    ok = pol.evaluate({"type": "set_floor", "geo": "US", "network": "Network D", "change_pct": -15}, cfg, 10, 0)
    too_big = pol.evaluate({"type": "set_floor", "geo": "US", "network": "Network D", "change_pct": -40}, cfg, 10, 0)
    bidder = pol.evaluate({"type": "set_floor", "geo": "US", "network": "Network A", "change_pct": -5}, cfg, 10, 0)
    assert ok.allowed and not too_big.allowed and not bidder.allowed


def test_rollback_is_exempt_from_step_limit_but_bounded_in_time():
    pol, cfg = setup()
    bad_push(cfg, tick=100)
    assert pol.evaluate({"type": "rollback_config", "to_version": 1}, cfg, 120, 0).allowed
    assert not pol.evaluate({"type": "rollback_config", "to_version": 1}, cfg, 100 + 500, 0).allowed
    assert not pol.evaluate({"type": "rollback_config", "to_version": 9}, cfg, 120, 0).allowed
    assert diff_configs(build_candidate({"type": "rollback_config", "to_version": 1}, cfg), cfg.get(1)) == []


def test_kill_switch_and_rate_limit_block_changes_but_not_escalation():
    pol, cfg = setup()
    action = {"type": "set_floor", "geo": "US", "network": "Network D", "change_pct": -10}
    assert not pol.evaluate(action, cfg, 10, recent_agent_changes=3).allowed
    pol = Policy(copy.deepcopy(pol.data))
    pol.kill_switch = True
    assert not pol.evaluate(action, cfg, 10, 0).allowed
    assert pol.evaluate({"type": "escalate", "team": "x"}, cfg, 10, 0).allowed


def test_disabling_a_big_network_needs_approval():
    pol, cfg = setup()
    action = {"type": "disable_network", "geo": "US", "network": "Network D"}
    big = pol.evaluate(action, cfg, 10, 0, network_revenue_share=0.42)
    small = pol.evaluate(action, cfg, 10, 0, network_revenue_share=0.12)
    assert big.allowed and big.needs_approval
    assert small.allowed and not small.needs_approval
    cand = build_candidate(action, cfg)
    assert cand["enabled"]["US|banner"]["Network D"] is False and cand["enabled"]["IN|banner"]["Network D"] is True


def test_unknown_action_is_rejected():
    pol, cfg = setup()
    assert not pol.evaluate({"type": "delete_everything"}, cfg, 10, 0).allowed
