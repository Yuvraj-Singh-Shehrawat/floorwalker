# Floorwalker eval results

Generated 2026-11-10 16:59 UTC by `make eval-llm` (25 incident runs, 20 clean runs, 255s).

**Agent mode: `llm`.** The LLM picks tools and proposes the fix; all arithmetic is done by deterministic tools.

All demand is simulated. Times are sim-minutes.

## Headline

| Metric | Floorwalker | Static alert (geo revenue -20% vs trailing hour) |
|---|---|---|
| Incidents detected | 100% | 76% |
| Median time to detect | 3 min | 6 min (when it fires) |
| False alarms in 80 clean sim-hours | 0 | 0 alerting minutes |
| Root cause correct | 100% | n/a (alerts do not diagnose) |
| Action appropriate | 92% | n/a |
| Median time to fix live (when a fix is promoted) | 20 min | n/a |

## By incident type

| Incident | Runs | Detected | Root cause | Action OK | Outcomes | Loss, no agent | Loss, agent | Avoided |
|---|---|---|---|---|---|---|---|---|
| timeout_misconfig | 5 | 100% | 100% | 100% | resolved 5 | $25.87 | $5.48 | 79% |
| bad_floor_push | 5 | 100% | 100% | 100% | resolved 5 | $56.41 | $11.69 | 79% |
| network_latency_spike | 5 | 100% | 100% | 100% | resolved 4, reverted 1 | $30.56 | $12.91 | 58% |
| demand_drop | 5 | 100% | 100% | 60% | escalated 5 | $9.35 | $9.35 | 0% |
| traffic_drop | 5 | 100% | 100% | 100% | escalated 5 | $75.83 | $75.83 | 0% |

Loss = simulated USD below baseline in the affected geo over the 90 sim-min after injection, summed over seeds. For escalated incidents the agent makes no change, so loss is expected to match (differences are sampling noise).

## How to read this

- `resolved`: fix won its canary and was promoted. `reverted`: canary did not beat control by the policy margin, nothing was promoted. `escalated`: no safe automatic fix; handed to a human.
- Escalating a traffic drop or a bidder-side demand drop is the correct action, not a miss.
- Per-run details are in `backend/evals/results.json`.

LLM usage: 89 calls, 245,726 input and 30,218 output tokens across 25 incidents.
