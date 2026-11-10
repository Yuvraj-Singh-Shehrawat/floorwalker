"""Incident benchmark with ground truth.  Run from backend/:  python -m evals.run_evals

For each incident type x seed, the same world is run twice:
  A. no agent       -> how much revenue the incident costs if nobody acts, and whether a
                       naive static alert (geo revenue 20% below its trailing hour) fires.
  B. with the agent -> detection, root cause, action, canary, recovery.
Plus clean runs with no incident to measure false alarms.

Writes evals/results.json and ../EVALS.md.

Scoring
  root cause correct  = category AND geo match ground truth, AND network when the truth names one.
  action appropriate  = config_change -> rollback_config; network_latency -> disable_network;
                        traffic_drop -> escalate; demand_drop -> set_floor (waterfall) or escalate (bidder).
  revenue loss        = sum over the 90 sim-min after injection of (baseline expected - actual)
                        in the affected geo. "Avoided" compares run B with run A.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app import chaos
from app.agent.runner import HeadlessClock, IncidentManager
from app.detection import Detector
from app.domain import BIDDERS, GEOS, SLICES, S_REV
from app.policy import Policy
from app.simulator import World, bootstrap_world

PRE_TICKS = 80        # clean history before injection (the static alert needs a trailing hour)
HORIZON = 90          # sim-min scored after injection
CLEAN_TICKS = 240
HERE = Path(__file__).resolve().parent


def geo_rev(world, geo: str, t0: int, t1: int) -> tuple[float, float]:
    idx = [i for i, (g, _f) in enumerate(SLICES) if g == geo]
    a, _ = world.metrics.window(t0, t1)
    e, _ = world.baseline.window(t0, t1)
    return float(a[idx, S_REV].sum()), float(e[idx, S_REV].sum())


def static_alert_fires(world, t: int) -> bool:
    """Naive alert: any geo's last-10-min revenue is 20% below its trailing-hour average."""
    for g in GEOS:
        now, _ = geo_rev(world, g, t - 9, t)
        ref, _ = geo_rev(world, g, t - 69, t - 10)
        if now < 0.8 * ref / 6.0:
            return True
    return False


def appropriate(truth: dict, action: dict) -> bool:
    cat, kind = truth["category"], action["type"]
    if cat == "config_change":
        return kind == "rollback_config"
    if cat == "network_latency":
        return kind == "disable_network"
    if cat == "traffic_drop":
        return kind == "escalate"
    if cat == "demand_drop":
        return kind == ("escalate" if truth["network"] in BIDDERS else "set_floor")
    return False


def run_incident(baseline, ctype: str, seed: int, mode: str) -> dict:
    # ---- A: nobody acts ----
    wa = World(seed=seed, start_tick=3000 + 97 * seed, baseline=baseline)
    wa.run(PRE_TICKS)
    ev = chaos.inject(wa, ctype)
    t_inj = wa.tick
    static_mttd = None
    for i in range(1, HORIZON + 1):
        wa.step()
        if static_mttd is None and static_alert_fires(wa, wa.tick):
            static_mttd = i
    a, e = geo_rev(wa, ev.geo, t_inj + 1, t_inj + HORIZON)
    loss_none = e - a

    # ---- B: with the agent (same seed => same incident) ----
    wb = World(seed=seed, start_tick=3000 + 97 * seed, baseline=baseline)
    det = Detector(baseline)
    mgr = IncidentManager(wb, Policy.load(), HeadlessClock(wb), mode=mode, auto_approve_after=5)
    for _ in range(PRE_TICKS):
        wb.step()
        det.check(wb.metrics, wb.tick)
    evb = chaos.inject(wb, ctype)
    assert (evb.geo, evb.network) == (ev.geo, ev.network), "runs diverged"
    inc = None
    while wb.tick < t_inj + HORIZON:
        wb.step()
        if inc is None:
            signals = det.check(wb.metrics, wb.tick)
            if signals:
                inc = mgr.open(signals)
                mgr.handle(inc)
    a, e = geo_rev(wb, ev.geo, t_inj + 1, t_inj + HORIZON)
    loss_agent = e - a

    row = {"type": ctype, "seed": seed, "geo": ev.geo, "network": ev.network, "truth": ev.truth,
           "detected": inc is not None, "static_alert_mttd": static_mttd,
           "loss_no_agent": round(loss_none, 3), "loss_with_agent": round(loss_agent, 3)}
    if inc is not None:
        rc, truth = inc["root_cause"] or {}, ev.truth
        fix_live = inc["recovery"]["minutes_detect_to_fix_live"] if inc["recovery"] else None
        row.update({
            "mttd": inc["opened_tick"] - t_inj, "status": inc["status"], "mode": inc["mode"],
            "diagnosed": {k: rc.get(k) for k in ("category", "geo", "network")},
            "root_cause_correct": (rc.get("category") == truth["category"] and rc.get("geo") == truth["geo"]
                                   and (truth["network"] is None or rc.get("network") == truth["network"])),
            "action": inc["action"]["type"], "action_appropriate": appropriate(truth, inc["action"]),
            "needed_approval": bool(inc["policy"] and inc["policy"]["needs_approval"]),
            "canary_lift_pct": inc["canary"]["lift_pct"] if inc["canary"] else None,
            "mttr": (inc["opened_tick"] - t_inj + fix_live) if fix_live is not None else None,
            "tool_calls": sum(1 for s in inc["steps"] if s["kind"] == "tool"),
            "usage": inc.get("usage"),
        })
    return row


def run_clean(baseline, seed: int) -> dict:
    w = World(seed=seed, start_tick=3000 + 131 * seed, baseline=baseline)
    det = Detector(baseline)
    w.run(PRE_TICKS)
    false_alarms = static = 0
    for _ in range(CLEAN_TICKS):
        w.step()
        if det.check(w.metrics, w.tick):
            false_alarms += 1
            det.mute(w.tick + 30)
        static += static_alert_fires(w, w.tick)
    return {"seed": seed, "false_alarms": false_alarms, "static_alert_minutes": static}


def pct(xs) -> str:
    xs = list(xs)
    return f"{100 * sum(xs) / len(xs):.0f}%" if xs else "n/a"


def med(xs) -> str:
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):.0f}" if xs else "n/a"


def write_report(rows: list[dict], clean: list[dict], mode: str, seeds: int, secs: float, path: Path) -> None:
    det = [r for r in rows if r["detected"]]
    lines = [
        "# Floorwalker eval results", "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `make {'eval-llm' if mode == 'llm' else 'eval'}` "
        f"({len(rows)} incident runs, {len(clean)} clean runs, {secs:.0f}s).", "",
        f"**Agent mode: `{mode}`.** " + (
            "These numbers come from the rule-based runbook, whose rules were written alongside these five "
            "incident types. They prove the plumbing works; they are **not** evidence the agent generalises. "
            "Set `ANTHROPIC_API_KEY` and run `make eval-llm` for numbers worth quoting."
            if mode == "fallback" else
            "The LLM picks tools and proposes the fix; all arithmetic is done by deterministic tools."), "",
        "All demand is simulated. Times are sim-minutes.", "",
        "## Headline", "",
        "| Metric | Floorwalker | Static alert (geo revenue -20% vs trailing hour) |", "|---|---|---|",
        f"| Incidents detected | {pct(r['detected'] for r in rows)} | "
        f"{pct(r['static_alert_mttd'] is not None for r in rows)} |",
        f"| Median time to detect | {med(r.get('mttd') for r in det)} min | "
        f"{med(r['static_alert_mttd'] for r in rows)} min (when it fires) |",
        f"| False alarms in {len(clean) * CLEAN_TICKS // 60} clean sim-hours | "
        f"{sum(c['false_alarms'] for c in clean)} | "
        f"{sum(c['static_alert_minutes'] for c in clean)} alerting minutes |",
        f"| Root cause correct | {pct(r['root_cause_correct'] for r in det)} | n/a (alerts do not diagnose) |",
        f"| Action appropriate | {pct(r['action_appropriate'] for r in det)} | n/a |",
        f"| Median time to fix live (when a fix is promoted) | {med(r.get('mttr') for r in det)} min | n/a |", "",
        "## By incident type", "",
        "| Incident | Runs | Detected | Root cause | Action OK | Outcomes | Loss, no agent | Loss, agent | Avoided |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for ctype in chaos.CATALOG:
        rs = [r for r in rows if r["type"] == ctype]
        d = [r for r in rs if r["detected"]]
        outcomes: dict[str, int] = {}
        for r in d:
            outcomes[r["status"]] = outcomes.get(r["status"], 0) + 1
        ln, la = sum(r["loss_no_agent"] for r in rs), sum(r["loss_with_agent"] for r in rs)
        avoided = f"{100 * (1 - la / ln):.0f}%" if ln > 0.5 else "n/a"
        lines.append(f"| {ctype} | {len(rs)} | {pct(r['detected'] for r in rs)} | "
                     f"{pct(r['root_cause_correct'] for r in d)} | {pct(r['action_appropriate'] for r in d)} | "
                     f"{', '.join(f'{k} {v}' for k, v in sorted(outcomes.items()))} | "
                     f"${ln:.2f} | ${la:.2f} | {avoided} |")
    lines += ["",
              "Loss = simulated USD below baseline in the affected geo over the 90 sim-min after injection, summed "
              "over seeds. For escalated incidents the agent makes no change, so loss is expected to match "
              "(differences are sampling noise).", "",
              "## How to read this", "",
              "- `resolved`: fix won its canary and was promoted. `reverted`: canary did not beat control by the "
              "policy margin, nothing was promoted. `escalated`: no safe automatic fix; handed to a human.",
              "- Escalating a traffic drop or a bidder-side demand drop is the correct action, not a miss.",
              "- Per-run details are in `backend/evals/results.json`.", ""]
    usage = [r["usage"] for r in det if r.get("usage")]
    if usage:
        lines += [f"LLM usage: {sum(u['llm_calls'] for u in usage)} calls, "
                  f"{sum(u['input_tokens'] for u in usage):,} input and "
                  f"{sum(u['output_tokens'] for u in usage):,} output tokens across {len(usage)} incidents.", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--clean", type=int, default=20)
    ap.add_argument("--mode", choices=["fallback", "llm"], default="fallback")
    ap.add_argument("--types", nargs="*", default=list(chaos.CATALOG))
    args = ap.parse_args()

    t0 = time.time()
    baseline = bootstrap_world(seed=0, cache_dir=".cache").baseline
    rows = []
    for ctype in args.types:
        for seed in range(args.seeds):
            row = run_incident(baseline, ctype, 1000 + seed, args.mode)
            rows.append(row)
            print(f"{ctype:22s} seed={seed} geo={row['geo']} net={row['network']} "
                  f"detected={row['detected']} status={row.get('status')} rc_ok={row.get('root_cause_correct')}")
    clean = [run_clean(baseline, 5000 + s) for s in range(args.clean)]
    secs = time.time() - t0
    (HERE / "results.json").write_text(json.dumps({"mode": args.mode, "rows": rows, "clean": clean}, indent=1))
    report = HERE.parent.parent / "EVALS.md"
    write_report(rows, clean, args.mode, args.seeds, secs, report)
    print(f"\nwrote {report} and {HERE / 'results.json'} in {secs:.0f}s")


if __name__ == "__main__":
    main()
