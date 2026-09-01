"""
Runs the scenario table from the assignment:

    Scenario   Answer Rate   Avg Talk Time
    A          20%           120 sec
    B          50%           90 sec
    C          70%           180 sec
    D          changing      changing

Each scenario builds a fresh store + N agents + M borrowers, runs the
dialer (progressive or predictive) with a pool of workers for a fixed
wall-clock duration, and reports utilization / calls initiated / calls
connected / abandoned / safety-controller decisions.
"""

import time
import uuid
import logging

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

logging.basicConfig(level=logging.WARNING)  # keep simulator output readable; flip to INFO for verbose traces


def build_campaign(num_agents: int, num_borrowers: int):
    store = InMemoryStore()
    for i in range(num_agents):
        store.add_agent(Agent(id=f"agent-{i}", name=f"Agent {i}", state=AgentState.AVAILABLE))
    for i in range(num_borrowers):
        store.add_borrower(Borrower(id=f"borrower-{i}", name=f"Borrower {i}", phone=f"+1555000{i:04d}"))
    return store


def run_progressive_scenario(name, answer_rate, avg_talk_time, num_agents=20,
                              num_borrowers=200, duration_seconds=6, num_workers=6,
                              provider_cls=ProviderA, provider_kwargs=None):
    provider_kwargs = provider_kwargs or {}
    store = build_campaign(num_agents, num_borrowers)
    provider = provider_cls(answer_rate=answer_rate, avg_talk_time=avg_talk_time, **provider_kwargs)
    safety = SafetyController(store)
    allocator = CallAllocator(store, safety_controller=safety)
    dialer = ProgressiveDialer(store, safety, allocator, provider)

    pool = DialerWorkerPool(dialer, num_workers=num_workers, poll_interval=0.03)
    start = time.time()
    pool.start()
    time.sleep(duration_seconds)
    pool.stop()
    elapsed = time.time() - start

    return _collect_metrics(name, store, safety, elapsed)


def run_predictive_scenario(name, answer_rate, avg_talk_time, num_agents=20,
                             num_borrowers=200, duration_seconds=6,
                             provider_cls=ProviderA, provider_kwargs=None):
    """Predictive mode driven from a single control loop (the pacing engine
    itself isn't parallelized across workers - only the resulting approved
    calls are placed - which mirrors 'one campaign pacing decision at a
    time' feeding a pool of allocator work)."""
    provider_kwargs = provider_kwargs or {}
    store = build_campaign(num_agents, num_borrowers)
    provider = provider_cls(answer_rate=answer_rate, avg_talk_time=avg_talk_time, **provider_kwargs)
    safety = SafetyController(store)
    allocator = CallAllocator(store, safety_controller=safety)
    pacing = PredictivePacingEngine(store, PacingStats(historical_answer_rate=answer_rate,
                                                        avg_talk_time_seconds=avg_talk_time))

    start = time.time()
    while time.time() - start < duration_seconds:
        suggested, reason = pacing.suggest_call_count()
        decision = safety.evaluate(requested_count=suggested, provider_healthy=provider.health_check())
        for _ in range(decision.approved_count):
            agent = store.reserve_next_available_agent()
            if agent is None:
                break
            borrower = store.reserve_next_borrower()
            if borrower is None:
                store.release_agent(agent.id, AgentState.AVAILABLE)
                break
            allocator.place_call(agent, borrower, provider)
        time.sleep(0.15)
    elapsed = time.time() - start

    return _collect_metrics(name, store, safety, elapsed)


def _collect_metrics(name, store, safety, elapsed):
    total_agents = len(store.all_agents())
    calls = store.all_calls()
    connected = sum(1 for c in calls if c.state in (CallState.COMPLETED,) or c.connected_at)
    failed = sum(1 for c in calls if c.state == CallState.FAILED)
    busy_agents = store.count_agents_by_state(AgentState.CONNECTED) + \
                  store.count_agents_by_state(AgentState.DIALING) + \
                  store.count_agents_by_state(AgentState.RESERVED)
    return {
        "scenario": name,
        "elapsed_seconds": round(elapsed, 2),
        "total_agents": total_agents,
        "calls_initiated": len(calls),
        "calls_connected": connected,
        "calls_failed": failed,
        "utilization_snapshot": f"{busy_agents}/{total_agents}",
        "safety_decisions": len(safety.decision_log),
        "safety_reductions": sum(1 for d in safety.decision_log if "Reduced" in d.reason),
    }
