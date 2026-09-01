"""
Predictive Pacing Engine (rule-based, not ML - per assignment, that's fine).

WHY THIS FORMULA:
Classic call-center pacing math. If we know the historical answer rate,
we know that to end up with N connected calls we need to START roughly
N / answer_rate calls (since many will go unanswered/fail). We also want
to avoid over-dialing while calls are already ringing and might still
connect, so we subtract calls currently in flight from the target.

    target_connects  = available_agents  (don't try to connect more than we can staff)
    suggested_starts = ceil(target_connects / answer_rate) - calls_ringing

This is intentionally simple and explainable ("why did you start 17 calls
instead of 10?" -> "10 available agents, ~65% historical answer rate,
7 calls already ringing that might still land -> ceil(10/0.65) - 7 = 8,
clamped by a max multiplier so pacing never runs away if answer_rate is
noisy/low").

CRITICAL: this class only ever returns a SUGGESTED count. It has no
reference to the allocator or the provider - structurally, it cannot place
a call. The suggestion is only ever acted on after SafetyController.evaluate()
approves it. This is what "the predictive algorithm should not have a way
to switch the safety mechanism off" means in code, not just in a diagram.
"""

import math
import logging
from dataclasses import dataclass, field
from core.models.agent import AgentState
from core.models.call import CallState

logger = logging.getLogger("smartdialer.predictive")


@dataclass
class PacingStats:
    """Rolling stats fed in from the simulator/campaign, or computed live
    from recent call history. Kept simple and explicit on purpose."""
    historical_answer_rate: float = 0.5   # 0..1
    avg_talk_time_seconds: float = 90.0
    max_pace_multiplier: float = 2.0       # hard ceiling vs. #available agents


class PredictivePacingEngine:
    def __init__(self, store, stats: PacingStats | None = None):
        self._store = store
        self.stats = stats or PacingStats()

    def suggest_call_count(self) -> tuple[int, str]:
        """Returns (suggested_count, human_readable_reason)."""
        available_agents = self._store.count_agents_by_state(AgentState.AVAILABLE)
        calls_ringing = self._store.count_calls_by_state(CallState.RINGING)

        if available_agents == 0:
            return 0, "No available agents - suggesting 0"

        answer_rate = max(0.05, min(1.0, self.stats.historical_answer_rate))  # guard div-by-~0
        target_connects = available_agents
        raw_suggestion = math.ceil(target_connects / answer_rate) - calls_ringing
        raw_suggestion = max(0, raw_suggestion)

        # hard ceiling: never suggest more than N x available agents,
        # regardless of how low the answer rate looks (protects against a
        # bad/noisy answer-rate estimate causing runaway over-dialing -
        # this is a self-imposed limit *inside* the pacing engine, on top
        # of the Safety Controller's independent limit).
        ceiling = math.ceil(available_agents * self.stats.max_pace_multiplier)
        suggestion = min(raw_suggestion, ceiling)

        reason = (
            f"available_agents={available_agents}, answer_rate={answer_rate:.2f}, "
            f"calls_ringing={calls_ringing} -> raw={raw_suggestion}, "
            f"capped_at_{self.stats.max_pace_multiplier}x={ceiling} -> suggesting {suggestion}"
        )
        logger.info(reason)
        return suggestion, reason
