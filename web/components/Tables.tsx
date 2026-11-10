"use client";

import { type AuditEntry, type State, pct } from "@/lib/api";

// A drop is flagged with an arrow as well as colour, so state never rides on colour alone.
function Delta({ v, warnAt = -8 }: { v: number; warnAt?: number }) {
  const bad = v <= warnAt;
  return (
    <span className="num" style={{ color: bad ? "var(--critical)" : "var(--ink)" }}>
      {bad ? "▼ " : ""}{pct(v)}
    </span>
  );
}

const ratioPct = (a: number, e: number) => (e > 0 ? (a / e - 1) * 100 : 0);

export function GeoTable({ state }: { state: State }) {
  return (
    <div className="card p-4">
      <div className="card-title">By geo · last 10 sim-min</div>
      <table className="mt-2 w-full">
        <thead><tr>
          <th className="th">Geo</th><th className="th text-right">Share</th><th className="th text-right">Revenue vs baseline</th>
          <th className="th text-right">Fill</th><th className="th text-right">Show</th><th className="th text-right">eCPM</th>
        </tr></thead>
        <tbody className="divide-y divide-line/60">
          {state.geos.map((g) => (
            <tr key={g.geo}>
              <td className="td font-medium">{g.geo}</td>
              <td className="td num text-right text-ink2">{(g.revenue_share * 100).toFixed(0)}%</td>
              <td className="td text-right"><Delta v={g.revenue_vs_baseline_pct} /></td>
              <td className="td num text-right">{(g.fill_rate * 100).toFixed(1)}%</td>
              <td className="td num text-right">{(g.show_rate * 100).toFixed(1)}%</td>
              <td className="td num text-right">${g.ecpm.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function NetworkTable({ state }: { state: State }) {
  return (
    <div className="card p-4">
      <div className="card-title">By network · last 10 sim-min</div>
      <table className="mt-2 w-full">
        <thead><tr>
          <th className="th">Network</th><th className="th text-right">Share</th><th className="th text-right">Response rate</th>
          <th className="th text-right">Avg price</th><th className="th text-right">Latency</th><th className="th text-right">Timeouts</th>
        </tr></thead>
        <tbody className="divide-y divide-line/60">
          {state.networks.map((n) => (
            <tr key={n.network}>
              <td className="td whitespace-nowrap">
                <div className="font-medium">{n.network}</div>
                <div className="text-[11px] text-ink3">{n.type}</div>
              </td>
              <td className="td num text-right text-ink2">{(n.revenue_share * 100).toFixed(0)}%</td>
              <td className="td text-right"><Delta v={ratioPct(n.response_rate, n.response_rate_expected)} warnAt={-10} /></td>
              <td className="td text-right"><Delta v={ratioPct(n.avg_price, n.avg_price_expected)} warnAt={-10} /></td>
              <td className="td num text-right">{n.latency_ms} ms</td>
              <td className="td num text-right" style={{ color: n.timeout_rate > 0.05 ? "var(--critical)" : undefined }}>
                {n.timeout_rate > 0.05 ? "▲ " : ""}{(n.timeout_rate * 100).toFixed(1)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-ink3">Response rate and price are shown relative to the seasonal baseline, across all geos.</p>
    </div>
  );
}

export function AuditLog({ entries }: { entries: AuditEntry[] }) {
  return (
    <div className="card p-4">
      <div className="card-title">Config audit log</div>
      <ul className="mt-2 divide-y divide-line/60">
        {entries.slice(0, 8).map((e) => (
          <li key={e.version} className="py-1.5">
            <details>
              <summary className="flex items-baseline gap-2 text-sm">
                <span className="num text-ink3">v{e.version}</span>
                <span className={`rounded px-1.5 py-0.5 text-[11px] ${e.actor === "floorwalker-agent" ? "bg-ink text-page" : "bg-raised text-ink2"}`}>{e.actor}</span>
                <span className="min-w-0 flex-1 truncate text-ink2">{e.note}</span>
                <span className="num text-xs text-ink3">{e.diff.length} values</span>
              </summary>
              <ul className="num ml-8 mt-1 max-h-40 overflow-auto text-[11px] text-ink2">
                {e.diff.slice(0, 30).map((d, i) => (
                  <li key={i}>{d.section} · {d.slice} · {d.network}: {String(d.old)} → {String(d.new)}</li>
                ))}
              </ul>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function PolicyCard({ policy: raw }: { policy: Record<string, unknown> | null }) {
  if (!raw) return null;
  const policy = raw as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const rows: [string, string][] = [
    ["Floor change per step", `±${policy.floor?.max_step_pct}% max`],
    ["Config changes", `${policy.rate_limit?.max_changes_per_sim_hour} per sim-hour max`],
    ["Canary first", `${(policy.canary?.fraction * 100).toFixed(0)}% of traffic for ${policy.canary?.duration_sim_min} sim-min`],
    ["Promote only if", `canary revenue/request ≥ ${policy.canary?.min_rpr_ratio}× control`],
    ["Human approval", `to disable a network with > ${(policy.network_disable?.approval_revenue_share * 100).toFixed(0)}% of a geo's revenue`],
    ["Rollback window", `changes from the last ${policy.rollback?.max_lookback_sim_min} sim-min only`],
    ["Kill switch", policy.kill_switch ? "ON: agent cannot change config" : "off"],
  ];
  return (
    <div className="card p-4">
      <div className="card-title">Guardrails · policy.yaml</div>
      <dl className="mt-2 space-y-1.5">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-3 text-sm">
            <dt className="text-ink3">{k}</dt><dd className="text-right text-ink2">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-ink3">The LLM proposes; deterministic code checks every action against this file before it touches traffic.</p>
    </div>
  );
}
