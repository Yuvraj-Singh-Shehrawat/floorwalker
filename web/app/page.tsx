"use client";

import { useEffect, useRef, useState } from "react";
import ChaosPanel from "@/components/ChaosPanel";
import IncidentPanel from "@/components/IncidentPanel";
import RevenueChart from "@/components/RevenueChart";
import { AuditLog, GeoTable, NetworkTable, PolicyCard } from "@/components/Tables";
import { API_URL, type AuditEntry, type Catalog, type State, getCatalog, getConfig, getPolicy, getState, pct } from "@/lib/api";

function Tile({ label, value, sub, alert }: { label: string; value: string; sub?: string; alert?: boolean }) {
  return (
    <div className="card px-4 py-3">
      <div className="card-title">{label}</div>
      <div className="num mt-1 text-2xl font-semibold" style={{ color: alert ? "var(--critical)" : undefined }}>
        {alert ? "▼ " : ""}{value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-ink3">{sub}</div>}
    </div>
  );
}

export default function Page() {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [policy, setPolicy] = useState<Record<string, unknown> | null>(null);
  const [scope, setScope] = useState("ALL");
  const followedIncident = useRef<string | null>(null);

  // Poll live state once per second (one sim-minute at the default speed).
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await getState();
        if (!alive) return;
        setState(s);
        setError(null);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : "Backend unreachable");
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const running = state?.status === "running";
  useEffect(() => {
    if (!running) return;
    getCatalog().then(setCatalog).catch(() => {});
    getPolicy().then(setPolicy).catch(() => {});
    const load = () => getConfig().then((c) => setAudit(c.audit_log)).catch(() => {});
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [running]);

  // When a new incident opens, point the chart at the affected geo (once; the viewer can change it).
  const inc = state?.incident;
  useEffect(() => {
    if (inc && inc.closed_tick === null && inc.focus_geo && followedIncident.current !== inc.id) {
      followedIncident.current = inc.id;
      setScope(inc.focus_geo);
    }
  }, [inc]);

  if (!state || state.status !== "running") {
    return (
      <main className="mx-auto flex min-h-screen max-w-xl flex-col justify-center px-6">
        <h1 className="text-xl font-semibold">Floorwalker</h1>
        <p className="mt-2 text-ink2">
          {error
            ? `Cannot reach the backend at ${API_URL} (${error}). Start it with "make backend".`
            : "Warming up the simulator: replaying a day of traffic to learn the seasonal baseline. About ten seconds."}
        </p>
      </main>
    );
  }

  const k = state.kpis;
  return (
    <main className="mx-auto max-w-[1400px] px-4 pb-10 pt-5 lg:px-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Floorwalker</h1>
          <p className="text-sm text-ink2">An AI agent on call for a mobile game&apos;s ad revenue: detects incidents, finds the root cause, ships guarded fixes.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="num rounded bg-surface px-2 py-1 text-ink2">{state.sim_time}</span>
          <span className="rounded bg-surface px-2 py-1 text-ink2">
            Agent: {state.agent_mode === "llm" ? `LLM · ${state.model}` : "rule-based runbook (no API key)"}
          </span>
          <span className="rounded bg-surface px-2 py-1 text-ink2">Config v{state.config_version}</span>
          {state.tracing && <span className="rounded bg-surface px-2 py-1 text-ink2">Tracing on</span>}
          {state.kill_switch && <span className="rounded px-2 py-1 font-semibold" style={{ background: "var(--critical)" }}>⛔ Kill switch on</span>}
        </div>
      </header>

      <div className="mt-3 rounded-md border border-line bg-surface px-3 py-2 text-xs text-ink2">
        <strong className="text-ink">Simulated environment.</strong> Traffic, bids and the six ad networks are synthetic and fictional.
        One real second is one sim-minute. Detection, root-cause maths and guardrails are deterministic code; the agent decides what to check and what to do.
        {error && <span className="ml-2" style={{ color: "var(--warning)" }}>⚠ Connection lost, retrying…</span>}
      </div>

      <section className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="ARPDAU run-rate" value={`$${k.arpdau_run_rate.toFixed(4)}`} sub={`baseline $${k.arpdau_baseline.toFixed(4)} · ${k.total_dau.toLocaleString()} DAU`} />
        <Tile label="Revenue vs baseline" value={pct(k.revenue_vs_baseline_pct_10m)} sub={`last 10 sim-min · ${pct(k.revenue_vs_baseline_pct_60m)} last hour`} alert={k.revenue_vs_baseline_pct_10m <= -5} />
        <Tile label="Fill rate" value={`${(k.fill_rate * 100).toFixed(1)}%`} sub={`show rate ${(k.show_rate * 100).toFixed(1)}%`} />
        <Tile label="eCPM" value={`$${k.ecpm.toFixed(2)}`} sub="blended, all formats" />
      </section>

      <section className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="space-y-3 lg:col-span-2">
          <RevenueChart state={state} scope={scope} onScope={setScope} followed={inc?.focus_geo ?? null} />
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            <GeoTable state={state} />
            <NetworkTable state={state} />
          </div>
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            <AuditLog entries={audit} />
            <PolicyCard policy={policy} />
          </div>
        </div>
        <div className="space-y-3">
          <ChaosPanel state={state} catalog={catalog} />
          <IncidentPanel state={state} />
        </div>
      </section>
    </main>
  );
}
