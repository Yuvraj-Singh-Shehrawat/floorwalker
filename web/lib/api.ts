// Typed client for the Floorwalker backend. Shapes mirror backend/app/main.py -> Runtime.state().

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

export type Step = { tick: number; time: string; kind: string; title: string; detail: unknown };

export type Incident = {
  id: string;
  status: "investigating" | "needs_approval" | "canary" | "verifying" | "resolved" | "reverted" | "escalated";
  mode: "llm" | "fallback";
  opened_tick: number;
  opened_label: string;
  closed_tick: number | null;
  focus_geo: string | null;
  signals: { series: string; geo: string; format: string; network: string | null; z: number; change_pct: number }[];
  impact: { total_change_pct: number; focus_geo_change_pct: number };
  steps: Step[];
  root_cause: { category: string; geo: string | null; network: string | null; explanation: string } | null;
  action: Record<string, unknown> | null;
  policy: { allowed: boolean; needs_approval: boolean; checks: { rule: string; passed: boolean; detail: string }[] } | null;
  canary: { control_rpr: number; canary_rpr: number; lift_pct: number; promoted: boolean; duration_sim_min: number } | null;
  recovery: { geo: string; before_fix_vs_baseline_pct: number; after_fix_vs_baseline_pct: number; minutes_detect_to_fix_live: number } | null;
  postmortem: { narrative: string; follow_up: string; action: string | null } | null;
  usage: { input_tokens: number; output_tokens: number; llm_calls: number } | null;
  truth?: { type: string; category: string; geo: string; network: string | null };
};

export type State = {
  status: "warming_up" | "running" | "error";
  tick: number;
  sim_time: string;
  sim_speed: number;
  agent_mode: "llm" | "fallback";
  model: string;
  tracing: boolean;
  kill_switch: boolean;
  config_version: number;
  kpis: {
    arpdau_run_rate: number; arpdau_baseline: number; revenue_vs_baseline_pct_60m: number;
    revenue_vs_baseline_pct_10m: number; fill_rate: number; show_rate: number; ecpm: number; total_dau: number;
  };
  series: {
    tick: number[]; label: string[]; revenue: number[]; revenue_expected: number[];
    fill: number[]; fill_expected: number[]; ecpm: number[]; ecpm_expected: number[];
    geo: Record<string, { revenue: number[]; revenue_expected: number[] }>;
  };
  geos: { geo: string; revenue_vs_baseline_pct: number; revenue_share: number; fill_rate: number; show_rate: number; ecpm: number }[];
  networks: {
    network: string; type: string; revenue_share: number; revenue_vs_baseline_pct: number;
    response_rate: number; response_rate_expected: number; avg_price: number; avg_price_expected: number;
    latency_ms: number; timeout_rate: number;
  }[];
  canary: { incident_id: string; fraction: number; elapsed_sim_min: number; duration_sim_min: number; control_rpr: number; canary_rpr: number } | null;
  incident_marks: { id: string; start_tick: number; end_tick: number | null; status: string }[];
  incident: Incident | null;
  chaos: { available: boolean; reason: string };
  reset_in_sim_min: number | null;
};

export type Catalog = { types: { type: string; label: string; kind: string; description: string }[]; geos: string[]; networks: string[] };
export type AuditEntry = { version: number; tick: number; actor: string; note: string; diff: { section: string; slice: string; network: string; old: unknown; new: unknown }[] };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store", ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const body = await res.json(); if (body?.detail) msg = String(body.detail); } catch { /* keep status text */ }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const getState = () => request<State>("/api/state");
export const getCatalog = () => request<Catalog>("/api/chaos/catalog");
export const getConfig = () => request<{ version: number; audit_log: AuditEntry[] }>("/api/config");
export const getPolicy = () => request<Record<string, unknown>>("/api/policy");
export const injectChaos = (type: string, geo: string | null) =>
  request<{ ok: boolean; injected_at: string }>("/api/chaos", { method: "POST", body: JSON.stringify({ type, geo }) });
export const sendApproval = (id: string, approved: boolean) =>
  request<{ ok: boolean }>(`/api/incidents/${id}/approval`, { method: "POST", body: JSON.stringify({ approved }) });

export const pct = (v: number, digits = 1) => `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
