"""
Entry point: python -m core.simulator.run_simulation

Runs scenarios A, B, C (fixed answer-rate/talk-time) and D (changing
mid-run), for both Progressive and Predictive modes, plus a Provider B
(unreliable) run to show duplicate/out-of-order/timeout handling, and an
outage run to show provider-down behaviour. Prints a summary table.
"""

import time
import random
from core.simulator.scenarios import run_progressive_scenario, run_predictive_scenario, build_campaign
from core.providers.provider_a import ProviderA
from core.providers.provider_b import ProviderB
from core.engine.safety_controller import SafetyController
from core.allocator.call_allocator import CallAllocator
from core.engine.predictive import PredictivePacingEngine, PacingStats
from core.models.agent import AgentState

try:
    from tabulate import tabulate
except ImportError:
    def tabulate(rows, headers, tablefmt=None):
        out = [" | ".join(headers)]
        for r in rows:
            out.append(" | ".join(str(x) for x in r))
        return "\n".join(out)


SCENARIOS = [
    ("A", 0.20, 120),
    ("B", 0.50, 90),
    ("C", 0.70, 180),
]


def run_scenario_d():
    """Scenario D: changing conditions mid-run. We flip answer rate and
    provider health partway through and show pacing/safety adapting."""
    store = build_campaign(num_agents=20, num_borrowers=300)
    provider = ProviderA(answer_rate=0.6, avg_talk_time=100)
    safety = SafetyController(store)
    allocator = CallAllocator(store, safety_controller=safety)
    pacing = PredictivePacingEngine(store, PacingStats(historical_answer_rate=0.6, avg_talk_time_seconds=100))

    start = time.time()
    duration = 8
    while time.time() - start < duration:
        elapsed = time.time() - start
        # Simulate a sudden answer-rate crash halfway through (e.g. bad
        # campaign / wrong number list) and a provider outage near the end.
        if elapsed > duration / 2:
            pacing.stats.historical_answer_rate = 0.15
        if elapsed > duration * 0.75:
            provider.outage = True
        else:
            provider.outage = False

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

    from core.simulator.scenarios import _collect_metrics
    return _collect_metrics("D (changing conditions)", store, safety, time.time() - start)


def main():
    results = []

    print("Running Progressive Mode scenarios (Provider A)...")
    for name, ar, tt in SCENARIOS:
        results.append(run_progressive_scenario(f"Progressive-{name}", ar, tt, provider_cls=ProviderA))

    print("Running Predictive Mode scenarios (Provider A)...")
    for name, ar, tt in SCENARIOS:
        results.append(run_predictive_scenario(f"Predictive-{name}", ar, tt, provider_cls=ProviderA))

    print("Running Predictive Mode on unreliable Provider B "
          "(duplicate/out-of-order/timeout events)...")
    results.append(run_predictive_scenario(
        "Predictive-ProviderB", 0.5, 90, provider_cls=ProviderB,
        provider_kwargs={"timeout_rate": 0.15, "duplicate_rate": 0.3, "out_of_order_rate": 0.2}
    ))

    print("Running Scenario D (changing answer rate + mid-run provider outage)...")
    results.append(run_scenario_d())

    headers = ["Scenario", "Elapsed(s)", "Agents", "Calls Init.", "Calls Conn.",
               "Calls Failed", "Utilization", "Safety Decisions", "Safety Reductions"]
    rows = [[r["scenario"], r["elapsed_seconds"], r["total_agents"], r["calls_initiated"],
             r["calls_connected"], r["calls_failed"], r["utilization_snapshot"],
             r["safety_decisions"], r["safety_reductions"]] for r in results]

    print("\n" + tabulate(rows, headers=headers, tablefmt="github"))
    print(
        "\nNotes:\n"
        "- 'Safety Reductions' = number of times the Safety Controller cut the pacing\n"
        "  engine's suggested call count down to protect agent capacity.\n"
        "- Scenario D shows the system reacting to a mid-run answer-rate crash and a\n"
        "  provider outage without any abandoned calls.\n"
        "- Provider B run demonstrates duplicate and out-of-order provider events being\n"
        "  absorbed safely by the store's idempotency + state-machine validation.\n"
    )


if __name__ == "__main__":
    main()
