"use client";

import { useState } from "react";
import { type Catalog, type State, injectChaos } from "@/lib/api";

export default function ChaosPanel({ state, catalog }: { state: State; catalog: Catalog | null }) {
  const [type, setType] = useState("bad_floor_push");
  const [geo, setGeo] = useState("random");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const fire = async () => {
    setBusy(true);
    setNote(null);
    try {
      const res = await injectChaos(type, geo === "random" ? null : geo);
      setNote(`Fault injected at ${res.injected_at}. The agent has not been told what or where.`);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const order = ["bad_floor_push", "timeout_misconfig", "network_latency_spike", "traffic_drop", "demand_drop"];
  const types = catalog ? [...catalog.types].sort((a, b) => order.indexOf(a.type) - order.indexOf(b.type)) : [];

  return (
    <div className="card p-4">
      <div className="card-title">Break something</div>
      <p className="mt-1 text-sm text-ink2">Inject a fault into the ad stack, then watch the agent find and handle it.</p>
      <div className="mt-3 space-y-1.5" role="radiogroup" aria-label="Fault type">
        {types.map((t) => (
          <label key={t.type}
            className={`block cursor-pointer rounded-md border px-3 py-2 ${type === t.type ? "border-ink3 bg-raised" : "border-line hover:border-ink3"}`}>
            <input type="radio" name="chaos" className="sr-only" checked={type === t.type} onChange={() => setType(t.type)} />
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{t.label}</span>
              <span className="rounded bg-page px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink3">
                {t.kind === "config" ? "bad config push" : "external"}
              </span>
            </div>
            {type === t.type && <div className="mt-1 text-xs text-ink2">{t.description}</div>}
          </label>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <label className="text-xs text-ink3" htmlFor="geo">Geo</label>
        <select id="geo" value={geo} onChange={(e) => setGeo(e.target.value)}
          className="rounded-md border border-line bg-raised px-2 py-1.5 text-sm">
          <option value="random">Random</option>
          {catalog?.geos.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
        <button className="btn-primary flex-1" onClick={fire} disabled={busy || !state.chaos.available}>
          {busy ? "Injecting…" : "Break it"}
        </button>
      </div>
      <p className="mt-2 min-h-[1.25rem] text-xs text-ink3" aria-live="polite">
        {!state.chaos.available ? state.chaos.reason : note ?? "One fault at a time. US has the most revenue, so faults there are easiest to see."}
      </p>
    </div>
  );
}
