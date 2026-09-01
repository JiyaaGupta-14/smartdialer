"""
Progressive Dialer: 1 available agent -> at most 1 outbound call.

Never creates more agent-bound outbound calls than there are available
agents, because it only ever asks the Safety Controller to approve calls
1-for-1 against agents it has *already atomically reserved*. It still goes
through the Safety Controller (never talks to the allocator directly) so
provider-health and abandonment protections apply uniformly across modes.
"""

import logging
from core.models.agent import AgentState

logger = logging.getLogger("smartdialer.progressive")


class ProgressiveDialer:
    def __init__(self, store, safety_controller, allocator, provider):
        self._store = store
        self._safety = safety_controller
        self._allocator = allocator
        self._provider = provider

    def run_once(self) -> int:
        """One dialing pass: reserve one available agent, reserve one
        borrower, ask the safety controller to approve 1 call, place it
        if approved. Returns 1 if a call was placed, else 0.
        Safe to call concurrently from multiple worker threads because
        agent/borrower reservation is atomic in the store."""
        agent = self._store.reserve_next_available_agent()
        if agent is None:
            return 0  # no agents free right now

        borrower = self._store.reserve_next_borrower()
        if borrower is None:
            # nobody to call - release the agent back immediately
            self._store.release_agent(agent.id, AgentState.AVAILABLE)
            return 0

        decision = self._safety.evaluate(requested_count=1, provider_healthy=self._provider.health_check())
        if decision.approved_count < 1:
            logger.info(f"Safety controller rejected call for agent {agent.id}: {decision.reason}")
            self._store.release_agent(agent.id, AgentState.AVAILABLE)
            self._store.requeue_borrower(borrower.id)
            return 0

        self._allocator.place_call(agent, borrower, self._provider)
        return 1
