"""
Thread-safe in-memory "database".

WHY A SINGLE LOCK AND NOT REDIS/POSTGRES:
At the scale this prototype needs to prove correctness at (simulating up to
a few thousand agents on one machine), a single process-wide RLock around
short, simple critical sections is enough to guarantee atomicity, and it is
trivial to reason about and test. The ADR.md file explains what changes at
real multi-machine scale (compare-and-set rows in Postgres, or a Redis
SETNX-style lock / distributed lock manager) and why we did not build that
here: it would add operational complexity without proving anything extra
about the *logic* being graded.

HOW WE PREVENT TWO WORKERS RESERVING THE SAME AGENT:
`reserve_next_available_agent()` and `reserve_agent()` do a read-check-write
as a single atomic operation under one lock. Two threads calling this at
"the same time" are serialized by the lock - one wins the compare-and-set
from AVAILABLE -> RESERVED, the other sees the agent is no longer AVAILABLE
and moves on to the next one (or gets None if none are left). This is the
same idea as an atomic UPDATE ... WHERE state = 'AVAILABLE' in SQL, or a
Redis WATCH/MULTI transaction - "reserve" is never a separate read then write.

IDEMPOTENCY / DUPLICATE & OUT-OF-ORDER EVENTS:
Every provider event has an event_id. `update_call_state()` checks
`processed_event_ids` first (dedupe), then validates the transition against
the Call state machine (out-of-order events that don't make sense from the
call's *current* state are rejected and logged, not applied).
"""

import threading
import time
import logging
from typing import Optional

from core.models.agent import Agent, AgentState, is_transition_allowed as agent_transition_ok
from core.models.call import Call, CallState, is_transition_allowed as call_transition_ok
from core.models.borrower import Borrower, BorrowerStatus

logger = logging.getLogger("smartdialer.store")


class TransitionRejected(Exception):
    """Raised (and caught internally) when a state transition is invalid.
    Callers get a bool back from public methods; this is used internally
    so both agent and call updates share one code path."""


class InMemoryStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._agents: dict[str, Agent] = {}
        self._calls: dict[str, Call] = {}
        self._borrowers: dict[str, Borrower] = {}
        self._borrower_queue: list[str] = []
        self.event_log: list[dict] = []  # audit trail for debugging / tests

    # ---------- setup ----------
    def add_agent(self, agent: Agent):
        with self._lock:
            self._agents[agent.id] = agent

    def add_borrower(self, borrower: Borrower):
        with self._lock:
            self._borrowers[borrower.id] = borrower
            self._borrower_queue.append(borrower.id)

    # ---------- reads ----------
    def get_agent(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            return self._agents.get(agent_id)

    def get_call(self, call_id: str) -> Optional[Call]:
        with self._lock:
            return self._calls.get(call_id)

    def count_agents_by_state(self, state: AgentState) -> int:
        with self._lock:
            return sum(1 for a in self._agents.values() if a.state == state)

    def count_calls_by_state(self, state: CallState) -> int:
        with self._lock:
            return sum(1 for c in self._calls.values() if c.state == state)

    def all_agents(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def all_calls(self) -> list[Call]:
        with self._lock:
            return list(self._calls.values())

    # ---------- the critical section: agent reservation ----------
    def reserve_next_available_agent(self) -> Optional[Agent]:
        """Atomically find ONE available agent and flip it to RESERVED.
        This is the operation that must never let two callers get the
        same agent. It is a single read-check-write under one lock."""
        with self._lock:
            for agent in self._agents.values():
                if agent.state == AgentState.AVAILABLE:
                    self._transition_agent_locked(agent, AgentState.RESERVED)
                    return agent
            return None

    def reserve_agent(self, agent_id: str) -> bool:
        """Reserve a specific agent by id. Returns False if it wasn't
        AVAILABLE (i.e. someone else already grabbed it) - this is the
        compare-and-set that answers 'what if two workers see the same
        available agent at almost the same time'."""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or agent.state != AgentState.AVAILABLE:
                return False
            self._transition_agent_locked(agent, AgentState.RESERVED)
            return True

    def release_agent(self, agent_id: str, to_state: AgentState = AgentState.AVAILABLE) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return False
            return self._transition_agent_locked(agent, to_state)

    def set_agent_state(self, agent_id: str, to_state: AgentState) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return False
            return self._transition_agent_locked(agent, to_state)

    def _transition_agent_locked(self, agent: Agent, to_state: AgentState) -> bool:
        """Caller must already hold self._lock."""
        if not agent_transition_ok(agent.state, to_state):
            logger.warning(f"Rejected agent transition {agent.id}: {agent.state} -> {to_state}")
            return False
        agent.state = to_state
        agent.version += 1
        agent.updated_at = time.time()
        if to_state == AgentState.RESERVED:
            agent.reserved_at = time.time()
        self.event_log.append({
            "type": "agent_transition", "agent_id": agent.id,
            "to": to_state.value, "ts": time.time(),
        })
        return True

    # ---------- borrower allocation ----------
    def reserve_next_borrower(self) -> Optional[Borrower]:
        """Same pattern as agent reservation - atomic pop from the queue
        so two workers never grab the same borrower."""
        with self._lock:
            while self._borrower_queue:
                bid = self._borrower_queue.pop(0)
                borrower = self._borrowers.get(bid)
                if borrower and borrower.status == BorrowerStatus.PENDING:
                    borrower.status = BorrowerStatus.IN_PROGRESS
                    return borrower
            return None

    def mark_borrower_done(self, borrower_id: str):
        with self._lock:
            b = self._borrowers.get(borrower_id)
            if b:
                b.status = BorrowerStatus.DONE

    def requeue_borrower(self, borrower_id: str):
        """Used when a call fails early and the borrower should be retried."""
        with self._lock:
            b = self._borrowers.get(borrower_id)
            if b:
                b.status = BorrowerStatus.PENDING
                self._borrower_queue.append(borrower_id)

    # ---------- calls ----------
    def create_call(self, call: Call):
        with self._lock:
            self._calls[call.id] = call
            self.event_log.append({"type": "call_created", "call_id": call.id, "ts": time.time()})

    def update_call_state(self, call_id: str, to_state: CallState, event_id: Optional[str] = None) -> bool:
        """Idempotent, order-safe call state transition. Kept as a simple
        bool-returning wrapper for callers/tests that only care whether the
        event was accepted without error (duplicates count as accepted).
        See update_call_state_atomic() for callers that need to distinguish
        a genuinely NEW transition from a harmless duplicate replay -
        that distinction matters for anything that has side effects beyond
        the call record itself (e.g. the allocator's agent-state sync)."""
        accepted, _is_new = self.update_call_state_atomic(call_id, to_state, event_id)
        return accepted

    def update_call_state_atomic(self, call_id: str, to_state: CallState,
                                  event_id: Optional[str] = None) -> tuple[bool, bool]:
        """Returns (accepted, is_new_transition).
        - accepted=False -> transition was invalid/out-of-order and was rejected;
          call state is unchanged.
        - accepted=True, is_new_transition=False -> this exact event_id was
          already processed before (duplicate delivery); call state is
          unchanged from what it already was. Callers must NOT re-run any
          side effects (like moving an agent's state) for this event.
        - accepted=True, is_new_transition=True -> the transition was newly
          applied this call. Side effects should run.
        """
        with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                return False, False

            if event_id is not None:
                if event_id in call.processed_event_ids:
                    logger.info(f"Duplicate event {event_id} for call {call_id} - ignored")
                    return True, False  # already handled - accepted, but not new
                call.processed_event_ids.add(event_id)

            if not call_transition_ok(call.state, to_state):
                logger.warning(
                    f"Rejected out-of-order/invalid call transition "
                    f"{call.state} -> {to_state} for call {call_id} (event {event_id})"
                )
                return False, True  # rejected; was a new (non-duplicate) event

            same_state = call.state == to_state
            call.state = to_state
            call.updated_at = time.time()
            if to_state == CallState.CONNECTED:
                call.connected_at = time.time()
            if to_state in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
                call.ended_at = time.time()

            self.event_log.append({
                "type": "call_transition", "call_id": call_id,
                "to": to_state.value, "event_id": event_id, "ts": time.time(),
            })
            # Same-state re-application (no event_id given, so we couldn't
            # dedupe above) still shouldn't re-trigger side effects.
            return True, not same_state

    # ---------- crash recovery ----------
    def recover_stale_reservations(self, timeout_seconds: float = 30.0) -> int:
        """Simulates 'the system comes back' after a worker crash.
        Any agent stuck RESERVED/DIALING longer than timeout_seconds is
        released back to AVAILABLE, and its in-flight call (if any,
        non-terminal) is marked FAILED so the borrower can be retried.
        Returns number of agents recovered."""
        recovered = 0
        with self._lock:
            now = time.time()
            for agent in self._agents.values():
                if agent.state in (AgentState.RESERVED, AgentState.DIALING) and agent.reserved_at:
                    if now - agent.reserved_at > timeout_seconds:
                        if agent.current_call_id:
                            call = self._calls.get(agent.current_call_id)
                            if call and call.state not in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
                                self.update_call_state(call.id, CallState.FAILED, event_id=f"recovery-{call.id}")
                                self.requeue_borrower(call.borrower_id)
                        agent.current_call_id = None
                        self._transition_agent_locked(agent, AgentState.AVAILABLE)
                        recovered += 1
        return recovered
