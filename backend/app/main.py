"""FastAPI server: runs the sim clock, exposes state to the dashboard, accepts chaos requests.

Run locally:   uvicorn app.main:app --reload --port 8000     (or `make backend` from the repo root)

Env vars (all optional, see .env.example):
  ANTHROPIC_API_KEY        enables LLM mode; without it the rule-based runbook is used
  FLOORWALKER_MODEL        model id for the agent
  FLOORWALKER_AGENT_MODE   auto | llm | fallback
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST   enables tracing
  SIM_SPEED                sim-minutes per real minute (default 60 => one sim-minute per second)
  ADMIN_TOKEN              required in X-Admin-Token for kill switch / reset when set
  CORS_ORIGINS             comma-separated origins (default *)
  CHAOS_MAX_PER_HOUR       public rate limit on "Break something" (default 10)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import chaos
from .agent.runner import CLOSED, IncidentManager, LiveClock
from .configstore import default_config
from .detection import Detector
from .domain import (GEOS, NETWORKS, N_ATT, N_BID, N_LAT, N_RESP, N_REV, N_TO, SLICES, S_FILL, S_IMP,
                     S_REQ, S_REV, TOTAL_DAU, sim_time_label)
from .policy import Policy
from .simulator import bootstrap_world
from .tracing import Tracer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("floorwalker")

CHART_TICKS = 180
RESET_AFTER = 30          # sim-min after an incident closes before the demo environment resets
RESET_MUTE = 10           # sim-min of detector silence after a reset (lets the 5-minute windows flush)


class Runtime:
    def __init__(self) -> None:
        self.status = "warming_up"
        self.world = None
        self.detector: Detector | None = None
        self.policy = Policy.load()
        self.manager: IncidentManager | None = None
        self.tracer = Tracer()
        self.speed = float(os.getenv("SIM_SPEED", "60"))
        self.reset_at: int | None = None
        self.chaos_times: deque[float] = deque(maxlen=200)
        self._stop = threading.Event()
        self._state_cache: tuple[int, str, dict] | None = None

    # ---- lifecycle ----
    def boot(self) -> None:
        cache = os.getenv("FLOORWALKER_CACHE_DIR", ".cache")
        self.world = bootstrap_world(seed=int(os.getenv("SIM_SEED", "0")), cache_dir=cache)
        self.detector = Detector(self.world.baseline)
        self.manager = IncidentManager(self.world, self.policy, LiveClock(self.world), tracer=self.tracer)
        self.manager.on_close = self._on_incident_closed
        self.status = "running"
        log.info("world ready at %s, agent mode=%s, tracing=%s", sim_time_label(self.world.tick),
                 self.manager.mode, self.tracer.enabled)

    def loop(self) -> None:
        try:
            self.boot()
        except Exception:   # noqa: BLE001
            log.exception("boot failed")
            self.status = "error"
            return
        interval = 60.0 / self.speed
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.tick()
            except Exception:   # noqa: BLE001
                log.exception("tick failed")
            self._stop.wait(max(0.0, interval - (time.time() - t0)))

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        w = self.world
        t = w.step()
        if self.manager.active is not None:
            return
        ev = chaos.active_event(w)
        if self.reset_at is not None and t >= self.reset_at:
            self.reset_environment("Demo environment reset to baseline")
        elif ev is not None and ev.incident_id is None and t >= ev.end_tick:
            self.reset_environment("Chaos expired without detection; demo environment reset")
        else:
            signals = self.detector.check(w.metrics, t)
            if signals:
                inc = self.manager.open(signals)
                if ev is not None:
                    ev.incident_id = inc["id"]
                threading.Thread(target=self.manager.handle, args=(inc,), daemon=True).start()

    def _on_incident_closed(self, inc: dict) -> None:
        for ev in self.world.chaos_events:   # reveal ground truth only once the agent has committed to an answer
            if ev.incident_id == inc["id"]:
                inc["truth"] = {"type": ev.type, **ev.truth}
        t = self.world.tick
        self.detector.mute(t + RESET_AFTER + RESET_MUTE)
        self.reset_at = t + RESET_AFTER

    def reset_environment(self, note: str) -> None:
        w = self.world
        with w.lock:
            chaos.clear(w)
            w.end_canary()
            w.cfg.push(default_config(), actor="demo-reset", note=note, tick=w.tick)
            self.detector.mute(w.tick + RESET_MUTE)
            self.reset_at = None
        log.info(note)

    # ---- chaos gate ----
    def chaos_availability(self) -> dict:
        if self.status != "running":
            return {"available": False, "reason": "Simulator is warming up."}
        w = self.world
        if self.manager.active is not None:
            return {"available": False, "reason": f"{self.manager.active['id']} is in progress."}
        if self.reset_at is not None:
            wait = self.reset_at - w.tick + RESET_MUTE
            return {"available": False, "ready_in_sim_min": wait,
                    "reason": f"Cooling down: the demo environment resets, then is ready in about {wait} sim-min."}
        if chaos.active_event(w) is not None:
            return {"available": False, "reason": "A fault is active; waiting for the detector."}
        if w.tick <= self.detector.muted_until:
            return {"available": False, "ready_in_sim_min": self.detector.muted_until - w.tick + 1,
                    "reason": f"Cooling down; ready in {self.detector.muted_until - w.tick + 1} sim-min."}
        limit = int(os.getenv("CHAOS_MAX_PER_HOUR", "10"))
        recent = [x for x in self.chaos_times if x > time.time() - 3600]
        if len(recent) >= limit:
            return {"available": False, "reason": f"Rate limit: {limit} injected faults per hour."}
        return {"available": True, "reason": ""}

    # ---- dashboard payload ----
    def state(self) -> dict:
        if self.status != "running":
            return {"status": self.status}
        w = self.world
        inc = self.manager.active or (self.manager.incidents[-1] if self.manager.incidents else None)
        cache_key = (w.tick, f"{inc['id']}:{inc['status']}:{len(inc['steps'])}" if inc else "")
        if self._state_cache and self._state_cache[:2] == cache_key:
            return self._state_cache[2]
        with w.lock:
            t1 = w.tick
            t0 = max(w.metrics.first_tick or 0, t1 - CHART_TICKS + 1)
            As, _ = w.metrics.stack(t0, t1)
            Es, _ = w.baseline.stack(t0, t1)
            ticks = list(range(t0, t1 + 1))
            gi = np.array([GEOS.index(g) for g, _ in SLICES])

            def r(x, nd=3):
                return [round(float(v), nd) for v in x]

            series = {
                "tick": ticks, "label": [sim_time_label(t)[-9:-4] for t in ticks],
                "revenue": r(As[:, :, S_REV].sum(1)), "revenue_expected": r(Es[:, :, S_REV].sum(1)),
                "fill": r(As[:, :, S_FILL].sum(1) / As[:, :, S_REQ].sum(1), 4),
                "fill_expected": r(Es[:, :, S_FILL].sum(1) / Es[:, :, S_REQ].sum(1), 4),
                "ecpm": r(1000 * As[:, :, S_REV].sum(1) / As[:, :, S_IMP].sum(1)),
                "ecpm_expected": r(1000 * Es[:, :, S_REV].sum(1) / Es[:, :, S_IMP].sum(1)),
                "geo": {g: {"revenue": r(As[:, gi == i, S_REV].sum(1)),
                            "revenue_expected": r(Es[:, gi == i, S_REV].sum(1))} for i, g in enumerate(GEOS)},
            }
            a60, _ = w.metrics.window(t1 - 59, t1)
            e60, _ = w.baseline.window(t1 - 59, t1)
            a10, an10 = w.metrics.window(t1 - 9, t1)
            e10, en10 = w.baseline.window(t1 - 9, t1)
            base_arpdau = w.baseline.daily_revenue() / TOTAL_DAU
            ratio60 = float(a60[:, S_REV].sum() / e60[:, S_REV].sum())
            kpis = {
                "arpdau_run_rate": round(base_arpdau * ratio60, 5), "arpdau_baseline": round(base_arpdau, 5),
                "revenue_vs_baseline_pct_60m": round((ratio60 - 1) * 100, 1),
                "revenue_vs_baseline_pct_10m": round(float(a10[:, S_REV].sum() / e10[:, S_REV].sum() - 1) * 100, 1),
                "fill_rate": round(float(a10[:, S_FILL].sum() / a10[:, S_REQ].sum()), 4),
                "show_rate": round(float(a10[:, S_IMP].sum() / a10[:, S_FILL].sum()), 4),
                "ecpm": round(float(1000 * a10[:, S_REV].sum() / a10[:, S_IMP].sum()), 3),
                "total_dau": TOTAL_DAU,
            }
            geos = []
            for i, g in enumerate(GEOS):
                a, e = a10[gi == i].sum(0), e10[gi == i].sum(0)
                geos.append({"geo": g, "revenue_vs_baseline_pct": round(float(a[S_REV] / e[S_REV] - 1) * 100, 1),
                             "revenue_share": round(float(e[S_REV] / e10[:, S_REV].sum()), 3),
                             "fill_rate": round(float(a[S_FILL] / a[S_REQ]), 3),
                             "show_rate": round(float(a[S_IMP] / max(a[S_FILL], 1e-9)), 3),
                             "ecpm": round(float(1000 * a[S_REV] / max(a[S_IMP], 1e-9)), 3)})
            na, ne = an10.sum(0), en10.sum(0)
            nets = []
            for k, n in enumerate(NETWORKS):
                def rate(x, num, den):
                    return float(x[k, num] / max(x[k, den], 1e-9))
                nets.append({"network": n, "type": "bidder" if k < 3 else "waterfall",
                             "revenue_share": round(float(na[k, N_REV] / max(na[:, N_REV].sum(), 1e-9)), 3),
                             "revenue_vs_baseline_pct": round((na[k, N_REV] / max(ne[k, N_REV], 1e-9) - 1) * 100, 1),
                             "response_rate": round(rate(na, N_RESP, N_ATT), 3),
                             "response_rate_expected": round(rate(ne, N_RESP, N_ATT), 3),
                             "avg_price": round(rate(na, N_BID, N_RESP), 3),
                             "avg_price_expected": round(rate(ne, N_BID, N_RESP), 3),
                             "latency_ms": round(rate(na, N_LAT, N_ATT)),
                             "timeout_rate": round(rate(na, N_TO, N_ATT), 3)})
            can = w.canary
            canary = None
            if can is not None:
                c, tr = can.rpr()
                canary = {"incident_id": can.incident_id, "fraction": can.fraction,
                          "elapsed_sim_min": t1 - can.start_tick + 1,
                          "duration_sim_min": int(self.policy.canary["duration_sim_min"]),
                          "control_rpr": round(c * 1000, 4), "canary_rpr": round(tr * 1000, 4)}
            marks = [{"id": i["id"], "start_tick": i["opened_tick"], "end_tick": i["closed_tick"], "status": i["status"]}
                     for i in self.manager.incidents if (i["closed_tick"] or t1) >= t0]
            payload = {
                "status": "running", "tick": t1, "sim_time": sim_time_label(t1), "sim_speed": self.speed,
                "agent_mode": self.manager.mode, "model": os.getenv("FLOORWALKER_MODEL", "claude-haiku-5-5"),
                "tracing": self.tracer.enabled, "kill_switch": self.policy.kill_switch,
                "config_version": w.cfg.version, "kpis": kpis, "series": series, "geos": geos, "networks": nets,
                "canary": canary, "incident_marks": marks, "incident": inc,
                "chaos": self.chaos_availability(),
                "reset_in_sim_min": (self.reset_at - t1) if self.reset_at else None,
            }
        self._state_cache = (*cache_key, payload)
        return payload


rt = Runtime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("FLOORWALKER_NO_LOOP") != "1":
        threading.Thread(target=rt.loop, daemon=True).start()
    yield
    rt.stop()


app = FastAPI(title="Floorwalker", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])


def _ready() -> None:
    if rt.status != "running":
        raise HTTPException(503, "Simulator is warming up")


def _admin(token: str | None) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if expected and token != expected:
        raise HTTPException(401, "X-Admin-Token required")


class ChaosRequest(BaseModel):
    type: str
    geo: str | None = None
    network: str | None = None


class ApprovalRequest(BaseModel):
    approved: bool


class KillSwitchRequest(BaseModel):
    enabled: bool


@app.get("/")
def root() -> dict:
    # This service is the API only; the dashboard is a separate app. Say so instead of a bare 404.
    return {"service": "Floorwalker API (simulated ad stack)", "status": rt.status,
            "endpoints": {"health": "/healthz", "state": "/api/state", "docs": "/docs"}}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": rt.status != "error", "status": rt.status}


@app.get("/api/state")
def get_state() -> dict:
    return rt.state()


@app.get("/api/chaos/catalog")
def chaos_catalog() -> dict:
    return {"types": [{"type": k, **v} for k, v in chaos.CATALOG.items()], "geos": GEOS, "networks": NETWORKS}


@app.post("/api/chaos")
def post_chaos(req: ChaosRequest) -> dict:
    _ready()
    gate = rt.chaos_availability()
    if not gate["available"]:
        raise HTTPException(409, gate["reason"])
    if req.type not in chaos.CATALOG or (req.geo is not None and req.geo not in GEOS):
        raise HTTPException(422, "Unknown chaos type or geo")
    ev = chaos.inject(rt.world, req.type, geo=req.geo, network=req.network)
    rt.chaos_times.append(time.time())
    # The response deliberately omits the target: watch the agent find it.
    return {"ok": True, "id": ev.id, "type": ev.type, "injected_at": sim_time_label(ev.start_tick)}


@app.get("/api/incidents")
def list_incidents() -> dict:
    _ready()
    return {"incidents": [{k: i.get(k) for k in ("id", "status", "mode", "opened_label", "opened_tick", "closed_tick",
                                                 "root_cause", "action", "impact", "truth")}
                          for i in reversed(rt.manager.incidents)]}


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    _ready()
    for i in rt.manager.incidents:
        if i["id"] == incident_id:
            return i
    raise HTTPException(404, "No such incident")


@app.post("/api/incidents/{incident_id}/approval")
def post_approval(incident_id: str, req: ApprovalRequest) -> dict:
    _ready()
    if not rt.manager.approve(incident_id, req.approved):
        raise HTTPException(409, "Incident is not waiting for approval")
    return {"ok": True}


@app.get("/api/config")
def get_config() -> dict:
    _ready()
    w = rt.world
    return {"version": w.cfg.version, "audit_log": w.cfg.audit_log(), "config": w.cfg.current()}


@app.get("/api/policy")
def get_policy() -> dict:
    return rt.policy.data


@app.post("/api/admin/kill-switch")
def post_kill_switch(req: KillSwitchRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    _admin(x_admin_token)
    rt.policy.kill_switch = req.enabled
    return {"ok": True, "kill_switch": rt.policy.kill_switch}


@app.post("/api/admin/reset")
def post_reset(x_admin_token: str | None = Header(default=None)) -> dict:
    _admin(x_admin_token)
    _ready()
    if rt.manager.active is not None:
        raise HTTPException(409, "An incident is in progress")
    rt.reset_environment("Manual demo reset")
    return {"ok": True}


__all__ = ["app", "rt", "CLOSED"]
