"""
SmartDialer control API.

Serves the frontend dashboard AND the REST API from one process, on one
port, so there's nothing to configure (no CORS, no second server, no
external/paid services - everything runs locally).

Run with:
    uvicorn api.server:app --reload
Then open http://127.0.0.1:8000 in a browser.
"""

import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.models.agent import Agent, AgentState
from core.models.borrower import Borrower
from core.models.call import CallState
from core.store.in_memory_store import InMemoryStore
from core.providers.provider_a import ProviderA
from core.providers.provider_b import ProviderB
from core.engine.safety_controller import SafetyController
from core.engine.progressive import ProgressiveDialer
from core.engine.predictive import PredictivePacingEngine, PacingStats
from core.allocator.call_allocator import CallAllocator
from core.workers.dialer_worker import DialerWorkerPool
from core.simulator.scenarios import run_progressive_scenario, run_predictive_scenario
from loadtest.load_test import run_load_test as _run_load_test_raw

SCENARIOS_CFG = {"A": (0.20, 120), "B": (0.50, 90), "C": (0.70, 180)}

app = FastAPI(title="SmartDialer Control API")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# --------------------------------------------------------------------------
# Live Campaign Manager - runs a real, continuous dialing campaign in the
# background (progressive worker pool, or a predictive control loop) and
# exposes a snapshot the frontend polls.
# --------------------------------------------------------------------------
class CampaignManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.mode = None
        self.store: Optional[InMemoryStore] = None
        self.safety: Optional[SafetyController] = None
        self.allocator: Optional[CallAllocator] = None
        self.provider = None
        self.worker_pool: Optional[DialerWorkerPool] = None
        self._predictive_thread: Optional[threading.Thread] = None
        self._predictive_stop = threading.Event()
        self._history_thread: Optional[threading.Thread] = None
        self._history_stop = threading.Event()
        self.history = deque(maxlen=240)  # ~4 min at 1s ticks
        self.started_at = 0.0

    def start(self, mode: str, provider_name: str, num_agents: int, num_borrowers: int,
              num_workers: int, answer_rate: float, avg_talk_time: float):
        with self._lock:
            if self.running:
                return {"ok": False, "error": "Campaign already running - stop it first."}

            self.store = InMemoryStore()
            for i in range(num_agents):
                self.store.add_agent(Agent(id=f"agent-{i}", name=f"Agent {i}", state=AgentState.AVAILABLE))
            for i in range(num_borrowers):
                self.store.add_borrower(Borrower(id=f"b-{i}", name=f"Borrower {i}", phone=f"+1555{i:06d}"))

            provider_cls = ProviderA if provider_name == "A" else ProviderB
            self.provider = provider_cls(answer_rate=answer_rate, avg_talk_time=avg_talk_time)
            self.safety = SafetyController(self.store)
            self.allocator = CallAllocator(self.store, safety_controller=self.safety)

            self.mode = mode
            self.running = True
            self.started_at = time.time()
            self.history.clear()
            self._predictive_stop.clear()
            self._history_stop.clear()

            if mode == "progressive":
                dialer = ProgressiveDialer(self.store, self.safety, self.allocator, self.provider)
                self.worker_pool = DialerWorkerPool(dialer, num_workers=num_workers, poll_interval=0.05)
                self.worker_pool.start()
            else:  # predictive
                pacing = PredictivePacingEngine(
                    self.store, PacingStats(historical_answer_rate=answer_rate, avg_talk_time_seconds=avg_talk_time)
                )
                self._predictive_thread = threading.Thread(
                    target=self._predictive_loop, args=(pacing,), daemon=True
                )
                self._predictive_thread.start()

            self._history_thread = threading.Thread(target=self._history_loop, daemon=True)
            self._history_thread.start()

            return {"ok": True}

    def _predictive_loop(self, pacing: PredictivePacingEngine):
        while not self._predictive_stop.is_set():
            suggested, reason = pacing.suggest_call_count()
            decision = self.safety.evaluate(requested_count=suggested, provider_healthy=self.provider.health_check())
            for _ in range(decision.approved_count):
                agent = self.store.reserve_next_available_agent()
                if agent is None:
                    break
                borrower = self.store.reserve_next_borrower()
                if borrower is None:
                    self.store.release_agent(agent.id, AgentState.AVAILABLE)
                    break
                self.allocator.place_call(agent, borrower, self.provider)
            time.sleep(0.2)

    def _history_loop(self):
        while not self._history_stop.is_set():
            if self.store:
                self.history.append(self._snapshot_metrics())
            time.sleep(1.0)

    def stop(self):
        with self._lock:
            if not self.running:
                return {"ok": False, "error": "No campaign running."}
            self.running = False
            self._predictive_stop.set()
            self._history_stop.set()
            if self.worker_pool:
                self.worker_pool.stop()
                self.worker_pool = None
            return {"ok": True}

    def _snapshot_metrics(self) -> dict:
        calls = self.store.all_calls()
        total_agents = len(self.store.all_agents())
        busy = (self.store.count_agents_by_state(AgentState.CONNECTED)
                + self.store.count_agents_by_state(AgentState.DIALING)
                + self.store.count_agents_by_state(AgentState.RESERVED))
        connected = sum(1 for c in calls if c.connected_at is not None)
        failed = sum(1 for c in calls if c.state == CallState.FAILED)
        utilization_pct = round(100 * busy / total_agents, 1) if total_agents else 0.0
        return {
            "ts": round(time.time() - self.started_at, 1),
            "utilization_pct": utilization_pct,
            "calls_initiated": len(calls),
            "calls_connected": connected,
            "calls_failed": failed,
        }

    def snapshot(self) -> dict:
        if not self.store:
            return {"running": False}
        agents = [{"id": a.id, "state": a.state.value} for a in self.store.all_agents()]
        recent_calls = sorted(self.store.all_calls(), key=lambda c: c.updated_at, reverse=True)[:15]
        calls = [{
            "id": c.id, "state": c.state.value, "borrower_id": c.borrower_id,
            "agent_id": c.agent_id, "provider": c.provider_name,
        } for c in recent_calls]
        recent_decisions = self.safety.decision_log[-15:][::-1] if self.safety else []
        decisions = [{"reason": d.reason, "approved": d.approved_count, "forced": d.forced_progressive}
                     for d in recent_decisions]
        metrics = self._snapshot_metrics()
        return {
            "running": self.running,
            "mode": self.mode,
            "agents": agents,
            "recent_calls": calls,
            "recent_decisions": decisions,
            "metrics": metrics,
            "history": list(self.history),
        }


campaign = CampaignManager()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class StartCampaignRequest(BaseModel):
    mode: str = "progressive"          # "progressive" | "predictive"
    provider: str = "A"                 # "A" | "B"
    num_agents: int = 20
    num_borrowers: int = 300
    num_workers: int = 6
    answer_rate: float = 0.5
    avg_talk_time: float = 90.0


# --------------------------------------------------------------------------
# API routes
# --------------------------------------------------------------------------
@app.post("/api/campaign/start")
def start_campaign(req: StartCampaignRequest):
    return campaign.start(req.mode, req.provider, req.num_agents, req.num_borrowers,
                           req.num_workers, req.answer_rate, req.avg_talk_time)


@app.post("/api/campaign/stop")
def stop_campaign():
    return campaign.stop()


@app.get("/api/campaign/snapshot")
def get_snapshot():
    return campaign.snapshot()


@app.post("/api/scenarios/run/{name}")
def run_scenario(name: str):
    name = name.upper()
    if name not in SCENARIOS_CFG:
        return {"ok": False, "error": f"Unknown scenario '{name}'. Use A, B, or C."}
    answer_rate, avg_talk_time = SCENARIOS_CFG[name]
    prog = run_progressive_scenario(f"Progressive-{name}", answer_rate, avg_talk_time, duration_seconds=4)
    pred = run_predictive_scenario(f"Predictive-{name}", answer_rate, avg_talk_time, duration_seconds=4)
    return {"ok": True, "progressive": prog, "predictive": pred}


@app.post("/api/loadtest/run")
def run_loadtest(agents: int = 1000):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_load_test_raw(num_agents=agents, num_workers=50, attempts_per_worker=200)
    return {"ok": True, "output": buf.getvalue()}


# --------------------------------------------------------------------------
# Serve the frontend
# --------------------------------------------------------------------------
@app.get("/")
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
