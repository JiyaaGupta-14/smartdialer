"""
Call Allocator: the only component that actually talks to a TelecomProvider.
Sits after the Safety Controller in the pipeline. Responsible for:
  - creating the Call record
  - moving the agent RESERVED -> DIALING
  - invoking provider.initiate_call()
  - translating provider events into Call/Agent state transitions via the store
  - detecting and reporting abandonment (connected call, no agent to hand to)
  - handling provider-level failures (timeout/outage) by releasing the
    agent and requeueing the borrower rather than leaving things stuck
"""

import uuid
import logging
from core.models.call import Call, CallState
from core.models.agent import AgentState
from core.providers.base import ProviderTimeoutError, ProviderUnavailableError

logger = logging.getLogger("smartdialer.allocator")

# Provider event string -> Call state
EVENT_TO_STATE = {
    "INITIATED": CallState.INITIATED,
    "RINGING": CallState.RINGING,
    "ANSWERED": CallState.ANSWERED,
    "CONNECTED": CallState.CONNECTED,
    "COMPLETED": CallState.COMPLETED,
    "FAILED": CallState.FAILED,
}


class CallAllocator:
    def __init__(self, store, safety_controller=None):
        self._store = store
        self._safety = safety_controller  # optional, used to report abandonment

    def place_call(self, agent, borrower, provider) -> Call:
        call = Call(
            id=f"call-{uuid.uuid4().hex[:10]}",
            borrower_id=borrower.id,
            agent_id=agent.id,
            provider_name=provider.name,
        )
        self._store.create_call(call)
        self._store.update_call_state(call.id, CallState.RESERVED)
        self._store.set_agent_state(agent.id, AgentState.DIALING)
        agent.current_call_id = call.id

        def on_event(call_id: str, event_type: str, event_id: str):
            self._handle_provider_event(call_id, event_type, event_id, agent.id)

        try:
            provider_call_id = provider.initiate_call(call.id, borrower.phone, on_event)
            call.provider_call_id = provider_call_id
        except (ProviderTimeoutError, ProviderUnavailableError) as e:
            logger.warning(f"Provider error placing call {call.id}: {e}")
            self._store.update_call_state(call.id, CallState.FAILED, event_id=f"setup-fail-{call.id}")
            self._store.release_agent(agent.id, AgentState.AVAILABLE)
            self._store.requeue_borrower(borrower.id)

        return call

    def _handle_provider_event(self, call_id: str, event_type: str, event_id: str, agent_id: str):
        to_state = EVENT_TO_STATE.get(event_type)
        if to_state is None:
            logger.warning(f"Unknown provider event '{event_type}' for call {call_id} - ignored")
            return

        accepted, is_new = self._store.update_call_state_atomic(call_id, to_state, event_id=event_id)
        if not accepted:
            # genuinely invalid/out-of-order transition - store already logged it.
            return
        if not is_new:
            # duplicate delivery of an event we already processed - the call
            # state didn't change again, so we must NOT re-run agent-state
            # side effects below (that's exactly what caused false
            # "abandoned call" reports the first time this was built and
            # tested against Provider B's duplicate-event behaviour).
            return

        call = self._store.get_call(call_id)
        agent = self._store.get_agent(agent_id)
        if call is None or agent is None:
            return

        # Keep agent lifecycle in sync with the call outcome.
        if to_state == CallState.CONNECTED:
            if agent.state == AgentState.DIALING:
                self._store.set_agent_state(agent.id, AgentState.CONNECTED)
            else:
                # Connected but agent isn't in DIALING anymore (e.g. it was
                # already released by a recovery sweep after a crash) -
                # this IS an abandoned-call scenario. Report it.
                logger.error(f"ABANDONED CALL: {call_id} connected but agent {agent_id} unavailable")
                if self._safety:
                    self._safety.record_abandoned_call()

        elif to_state in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
            if to_state == CallState.COMPLETED:
                self._store.set_agent_state(agent.id, AgentState.WRAP_UP)
                self._store.mark_borrower_done(call.borrower_id)
                # simple auto wrap-up: return to pool immediately in this
                # simulation (a real system would time-box WRAP_UP)
                self._store.set_agent_state(agent.id, AgentState.AVAILABLE)
            else:
                # call failed/cancelled before connecting - free the agent,
                # let the borrower be retried
                if agent.state in (AgentState.DIALING, AgentState.RESERVED):
                    self._store.set_agent_state(agent.id, AgentState.AVAILABLE)
                self._store.requeue_borrower(call.borrower_id)
            agent.current_call_id = None
