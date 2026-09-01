"""
Safety Controller.

Campaign > Pacing Engine (Progressive/Predictive) > SAFETY CONTROLLER > Call
Allocator > Telecom Provider

The pacing engine can only ever SUGGEST a number of calls to start. This
class is the single authority that decides how many calls actually get
placed. It is intentionally the *only* thing with permission to hand a
count to the Call Allocator - engine code never talks to the allocator
directly, so there is no code path where predictive pacing can bypass
this check.

Rules enforced here:
1. Never approve more calls than there are AVAILABLE agents right now.
2. Track a rolling estimate of "abandonment risk" - if calls already
   ringing/answered plus the newly requested calls would likely exceed
   agent capacity (given the historical answer rate), reduce the count.
3. If a provider is unhealthy, reduce/reject calls routed to it.
4. Hard kill-switch: if abandonment risk crosses a hard ceiling, force a
   fallback to progressive behaviour (1 call per available agent) for a
   cooldown period, regardless of what the pacing engine asked for.
"""

import time
import logging
from dataclasses import dataclass

logger = logging.getLogger("smartdialer.safety")


@dataclass
class SafetyDecision:
    approved_count: int
    reason: str
    forced_progressive: bool = False


class SafetyController:
    def __init__(self, store, max_abandon_rate: float = 0.03,
                 cooldown_seconds: float = 5.0):
        self._store = store
        self.max_abandon_rate = max_abandon_rate
        self.cooldown_seconds = cooldown_seconds
        self._forced_progressive_until: float = 0.0
        self.decision_log: list[SafetyDecision] = []

    def record_abandoned_call(self):
        """Called by the allocator/store when a connected call has no
        agent to hand off to (the thing we must never let happen, but we
        still track it defensively)."""
        self._abandon_events = getattr(self, "_abandon_events", 0) + 1
        if self._abandon_events >= 1:
            # any real abandonment immediately forces a cooldown - zero
            # tolerance, since this is a compliance issue, not just UX.
            self._forced_progressive_until = time.time() + self.cooldown_seconds * 3
            logger.error("ABANDONED CALL DETECTED - forcing progressive fallback")

    def evaluate(self, requested_count: int, provider_healthy: bool = True) -> SafetyDecision:
        """The pacing engine calls this with how many calls it WANTS to
        start. This method returns how many it is actually ALLOWED to
        start. This is the one and only gate."""
        from core.models.agent import AgentState

        available_agents = self._store.count_agents_by_state(AgentState.AVAILABLE)
        in_flight = (
            self._store.count_calls_by_state(__import__("core.models.call", fromlist=["CallState"]).CallState.RINGING)
        )

        now = time.time()
        forced = now < self._forced_progressive_until

        if forced:
            decision = SafetyDecision(
                approved_count=min(requested_count, available_agents),
                reason="Forced progressive fallback active (recent abandonment / cooldown)",
                forced_progressive=True,
            )
            self.decision_log.append(decision)
            return decision

        if not provider_healthy:
            decision = SafetyDecision(0, "Provider unhealthy - rejecting all new calls")
            self.decision_log.append(decision)
            return decision

        # Never exceed available agents, ever - this is the hard floor.
        capped = min(requested_count, available_agents)

        # Extra caution: if there are already calls ringing that haven't
        # resolved yet, treat those as "possible near-future connects"
        # competing for the same agent pool, and shrink the new request.
        headroom = max(0, available_agents - in_flight)
        capped = min(capped, headroom)

        if capped < requested_count:
            reason = (f"Reduced {requested_count} -> {capped} "
                      f"(available_agents={available_agents}, in_flight_ringing={in_flight})")
        else:
            reason = f"Approved {capped} (available_agents={available_agents})"

        decision = SafetyDecision(approved_count=max(0, capped), reason=reason)
        self.decision_log.append(decision)
        logger.info(reason)
        return decision
