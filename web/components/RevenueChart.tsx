"use client";

import { useMemo } from "react";
import { CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { State } from "@/lib/api";

type Props = { state: State; scope: string; onScope: (s: string) => void; followed: string | null };

const rolling = (xs: number[], w: number) =>
  xs.map((_, i) => {
    const part = xs.slice(Math.max(0, i - w + 1), i + 1);
    return part.reduce((a, b) => a + b, 0) / part.length;
  });

const STATUS_FILL: Record<string, string> = { resolved: "var(--good)", reverted: "var(--warning)", escalated: "var(--serious)" };

export default function RevenueChart({ state, scope, onScope, followed }: Props) {
  const s = state.series;
  const data = useMemo(() => {
    const src = scope === "ALL" ? { revenue: s.revenue, revenue_expected: s.revenue_expected } : s.geo[scope];
    const actual = rolling(src.revenue, 3);
    return s.tick.map((t, i) => ({ tick: t, actual: actual[i], expected: src.revenue_expected[i] }));
  }, [s, scope]);
  const first = s.tick[0];
  const last = s.tick[s.tick.length - 1];
  const labelOf = (t: number) => s.label[Math.max(0, Math.min(s.label.length - 1, t - first))] ?? "";
  const scopes = ["ALL", ...Object.keys(s.geo)];

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="card-title">Ad revenue per sim-minute</div>
          <div className="mt-1 text-sm text-ink2">
            {scope === "ALL" ? "Whole portfolio" : `Geo ${scope}`} · 3-minute average vs seasonal baseline
            {followed && scope === followed ? " · following the incident" : ""}
          </div>
        </div>
        <div className="flex gap-1" role="tablist" aria-label="Chart scope">
          {scopes.map((g) => (
            <button key={g} role="tab" aria-selected={scope === g} onClick={() => onScope(g)}
              className={`rounded px-2 py-1 text-xs font-medium ${scope === g ? "bg-ink text-page" : "bg-raised text-ink2 hover:text-ink"}`}>
              {g === "ALL" ? "All" : g}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-2 flex items-center gap-4 text-xs text-ink2">
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-5" style={{ background: "var(--series-1)" }} />Actual</span>
        <span className="flex items-center gap-1.5"><span className="inline-block w-5 border-t-2 border-dashed" style={{ borderColor: "var(--reference)" }} />Expected (same time yesterday)</span>
        <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-3 rounded-sm" style={{ background: "var(--critical)", opacity: 0.25 }} />Incident window</span>
      </div>

      <div className="mt-2 h-64" role="img" aria-label="Line chart of actual versus expected ad revenue with incident windows shaded">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="tick" type="number" domain={[first, last]} tickFormatter={labelOf} tickCount={7}
              tick={{ fill: "var(--ink-3)", fontSize: 11 }} stroke="var(--line)" />
            <YAxis width={52} tick={{ fill: "var(--ink-3)", fontSize: 11 }} stroke="var(--line)"
              tickFormatter={(v: number) => `$${v.toFixed(v < 1 ? 2 : 1)}`} domain={[0, "auto"]} />
            {state.incident_marks.map((m) => (
              <ReferenceArea key={m.id} x1={Math.max(first, m.start_tick)} x2={m.end_tick ?? last}
                fill={m.end_tick ? STATUS_FILL[m.status] ?? "var(--critical)" : "var(--critical)"} fillOpacity={0.14}
                stroke="none" label={{ value: m.id, position: "insideTopLeft", fill: "var(--ink-2)", fontSize: 10 }} />
            ))}
            <Tooltip isAnimationActive={false} cursor={{ stroke: "var(--ink-3)", strokeWidth: 1 }}
              contentStyle={{ background: "var(--raised)", border: "1px solid var(--line)", borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: "var(--ink-2)" }} itemStyle={{ color: "var(--ink)" }}
              labelFormatter={(t) => `${labelOf(Number(t))} UTC`}
              formatter={(v, name) => [`$${Number(v).toFixed(3)} / min`, name === "actual" ? "Actual" : "Expected"]} />
            <Line type="monotone" dataKey="expected" stroke="var(--reference)" strokeWidth={1.5} strokeDasharray="5 4"
              dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="actual" stroke="var(--series-1)" strokeWidth={2} dot={false}
              isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
