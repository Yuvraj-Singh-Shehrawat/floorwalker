"use client";

import { useState } from "react";
import { type Incident, type State, pct, sendApproval } from "@/lib/api";

// Status colours are reserved for state and always paired with an icon and a label.
const STATUS: Record<Incident["status"], { label: string; icon: string; color: string }> = {
  investigating: { label: "Investigating", icon: "◔", color: "var(--warning)" },
  needs_approval: { label: "Waiting for human approval", icon: "✋", color: "var(--warning)" },
  canary: { label: "Canary running", icon: "◑", color: "var(--series-1)" },
  verifying: { label: "Verifying recovery", icon: "◕", color: "var(--series-1)" },
  resolved: { label: "Resolved", icon: "✓", color: "var(--good)" },
  reverted: { label: "Canary lost · reverted", icon: "↺", color: "var(--warning)" },
  escalated: { label: "Escalated to a human", icon: "↗", color: "var(--serious)" },
};

const KIND_ICON: Record<string, string> = { system: "●", tool: "⚙", thought: "✎", decision: "◆", policy: "⛨", canary: "◑" };
const TOOL_BLURB: Record<string, string> = {
  get_config_changes: "Any config pushes just before the break?",
  decompose_revenue: "Which lever moved: traffic, fill, show rate or eCPM?",
  localize_revenue_change: "Which network, geo or format explains the change?",
  network_health: "Which network is sick, and what is the symptom?",
  query_metrics: "Time series, actual vs baseline",
};

function StepRow({ step }: { step: Incident["steps"][number] }) {
  const isTool = step.kind === "tool";
  const title = isTool ? `${step.title}()` : step.title;
  const body = (
    <div className="flex gap-2">
      <span className="mt-0.5 w-4 shrink-0 text-center text-ink3" aria-hidden>{KIND_ICON[step.kind] ?? "·"}</span>
      <div className="min-w-0 flex-1">
        <div className={`text-sm ${isTool ? "font-mono text-[13px]" : ""} ${step.kind === "thought" ? "italic text-ink2" : ""}`}>{title}</div>
        {isTool && <div className="text-xs text-ink3">{TOOL_BLURB[step.title] ?? ""}</div>}
      </div>
      <span className="num shrink-0 text-[11px] text-ink3">{step.time.slice(-9, -4)}</span>
    </div>
  );
  if (step.detail == null || typeof step.detail === "string") {
    return (
      <li className="py-1.5">
        {body}
        {typeof step.detail === "string" && <div className="ml-6 text-xs text-ink3">{step.detail}</div>}
      </li>
    );
  }
  return (
    <li className="py-1.5">
      <details>
        <summary className="rounded hover:bg-raised">{body}</summary>
        <pre className="ml-6 mt-1 max-h-56 overflow-auto rounded bg-page p-2 text-[11px] leading-snug text-ink2">
          {JSON.stringify(step.detail, null, 1)}
        </pre>
      </details>
    </li>
  );
}

export default function IncidentPanel({ state }: { state: State }) {
  const inc = state.incident;
  const [sending, setSending] = useState(false);
  if (!inc) {
    return (
      <div className="card p-4">
        <div className="card-title">Incident</div>
        <p className="mt-2 text-sm text-ink2">No incidents yet. All monitored series are within their normal range.</p>
        <p className="mt-1 text-xs text-ink3">255 series are checked every sim-minute against a seasonal baseline.</p>
      </div>
    );
  }
  const st = STATUS[inc.status];
  const rc = inc.root_cause;
  const can = state.canary && state.canary.incident_id === inc.id ? state.canary : null;
  const approve = async (ok: boolean) => {
    setSending(true);
    try { await sendApproval(inc.id, ok); } catch { /* state poll will show the outcome */ } finally { setSending(false); }
  };
  const truthMatch = inc.truth && rc
    ? rc.category === inc.truth.category && rc.geo === inc.truth.geo && (inc.truth.network === null || rc.network === inc.truth.network)
    : null;

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="card-title">Incident {inc.id}</div>
          <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold" style={{ color: st.color }}>
            <span aria-hidden>{st.icon}</span><span>{st.label}</span>
          </div>
        </div>
        <div className="text-right text-xs text-ink3">
          <div>Detected {inc.opened_label}</div>
          <div>{inc.mode === "llm" ? "LLM agent" : "Rule-based runbook"}</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
        <div className="rounded bg-raised px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-ink3">{inc.focus_geo ?? "Portfolio"} revenue at detection</div>
          <div className="num text-lg">{pct(inc.impact.focus_geo_change_pct)}</div>
        </div>
        <div className="rounded bg-raised px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-ink3">Strongest signal</div>
          <div className="truncate text-sm">
            {inc.signals[0] ? `${inc.signals[0].series}${inc.signals[0].network ? ` · ${inc.signals[0].network}` : ""} · ${inc.signals[0].geo}` : "n/a"}
          </div>
          <div className="num text-xs text-ink3">{inc.signals[0] ? `${pct(inc.signals[0].change_pct)}, z = ${inc.signals[0].z}` : ""}</div>
        </div>
      </div>

      {inc.status === "needs_approval" && (
        <div className="mt-3 rounded-md border px-3 py-2" style={{ borderColor: "var(--warning)" }}>
          <div className="text-sm font-medium">Guardrail: this network earns over 30% of the geo&apos;s revenue.</div>
          <div className="text-xs text-ink2">The agent may not disable it without a human. You are the human.</div>
          <div className="mt-2 flex gap-2">
            <button className="btn-primary" disabled={sending} onClick={() => approve(true)}>Approve</button>
            <button className="btn" disabled={sending} onClick={() => approve(false)}>Reject</button>
          </div>
        </div>
      )}

      {can && (
        <div className="mt-3 rounded bg-raised px-3 py-2">
          <div className="flex justify-between text-xs text-ink2">
            <span>Canary on {(can.fraction * 100).toFixed(0)}% of affected traffic</span>
            <span className="num">{Math.min(can.elapsed_sim_min, can.duration_sim_min)} / {can.duration_sim_min} min</span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-page">
            <div className="h-full rounded" style={{ width: `${Math.min(100, (100 * can.elapsed_sim_min) / can.duration_sim_min)}%`, background: "var(--series-1)" }} />
          </div>
          <div className="num mt-1.5 text-xs text-ink2">
            revenue / 1k requests: control ${can.control_rpr.toFixed(3)} · canary ${can.canary_rpr.toFixed(3)}
          </div>
        </div>
      )}

      {rc && (
        <div className="mt-3 border-t border-line pt-3">
          <div className="text-[11px] uppercase tracking-wide text-ink3">Root cause</div>
          <div className="text-sm font-medium">
            {rc.category.replace(/_/g, " ")}{rc.network ? ` · ${rc.network}` : ""}{rc.geo ? ` · ${rc.geo}` : ""}
          </div>
          <p className="mt-1 text-sm text-ink2">{rc.explanation}</p>
          {inc.postmortem?.action && <div className="mt-2 text-sm"><span className="text-ink3">Action: </span>{inc.postmortem.action}</div>}
          {inc.canary && (
            <div className="num mt-1 text-sm">
              <span className="font-sans text-ink3">Canary: </span>{pct(inc.canary.lift_pct)} revenue/request vs control
              <span className="font-sans text-ink3"> · {inc.canary.promoted ? "promoted" : "not promoted"}</span>
            </div>
          )}
          {inc.recovery && (
            <div className="num mt-1 text-sm">
              <span className="font-sans text-ink3">{inc.recovery.geo} revenue vs baseline: </span>
              {pct(inc.recovery.before_fix_vs_baseline_pct)} → {pct(inc.recovery.after_fix_vs_baseline_pct)}
              <span className="font-sans text-ink3"> · fix live {inc.recovery.minutes_detect_to_fix_live} min after detection</span>
            </div>
          )}
        </div>
      )}

      {inc.policy && (
        <details className="mt-3 border-t border-line pt-3">
          <summary className="text-[11px] uppercase tracking-wide text-ink3">
            Guardrail checks · {inc.policy.checks.filter((c) => c.passed).length}/{inc.policy.checks.length} passed ▾
          </summary>
          <ul className="mt-1 space-y-0.5 text-xs">
            {inc.policy.checks.map((c, i) => (
              <li key={i} className="flex gap-2">
                <span style={{ color: c.passed ? "var(--good)" : "var(--critical)" }}>{c.passed ? "✓ pass" : "✕ fail"}</span>
                <span className="font-mono text-ink3">{c.rule}</span><span className="text-ink2">{c.detail}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {inc.postmortem && (
        <div className="mt-3 border-t border-line pt-3">
          <div className="text-[11px] uppercase tracking-wide text-ink3">Postmortem</div>
          <p className="mt-1 text-sm text-ink2">{inc.postmortem.narrative}</p>
          <p className="mt-1 text-sm"><span className="text-ink3">Follow-up: </span>{inc.postmortem.follow_up}</p>
        </div>
      )}

      {inc.truth && (
        <div className="mt-3 rounded bg-raised px-3 py-2 text-xs">
          <span className="text-ink3">What was actually injected: </span>
          {inc.truth.type.replace(/_/g, " ")}{inc.truth.network ? ` · ${inc.truth.network}` : ""} · {inc.truth.geo}
          <span className="ml-2 font-medium" style={{ color: truthMatch ? "var(--good)" : "var(--critical)" }}>
            {truthMatch ? "✓ diagnosis matches" : "✕ diagnosis differs"}
          </span>
        </div>
      )}

      <div className="mt-3 border-t border-line pt-3">
        <div className="flex items-center justify-between">
          <div className="text-[11px] uppercase tracking-wide text-ink3">Agent timeline · click a step for its data</div>
          {inc.usage && <div className="num text-[11px] text-ink3">{inc.usage.llm_calls} LLM calls · {(inc.usage.input_tokens + inc.usage.output_tokens).toLocaleString()} tokens</div>}
        </div>
        <ol className="mt-1 divide-y divide-line/60">
          {inc.steps.map((s, i) => <StepRow key={i} step={s} />)}
        </ol>
      </div>
      {state.reset_in_sim_min != null && (
        <p className="mt-2 text-xs text-ink3">Demo environment resets to baseline in {state.reset_in_sim_min} sim-min.</p>
      )}
    </div>
  );
}
