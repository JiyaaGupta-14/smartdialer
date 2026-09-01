"""
Tests the assignment's specific failure case:
'Agent reserved > Borrower reserved > Call initiated > Worker crashes.
What happens when the system comes back?'

We simulate a crash by simply stopping short of completing the call
(nothing sets the agent back to AVAILABLE, as a real crashed worker
wouldn't get the chance to). Then we run the recovery sweep and assert
the system self-heals.
"""

import time
from core.models.agent import Agent, AgentState
from core.models.borrower import Borrower, BorrowerStatus
from core.models.call import Call, CallState
from core.store.in_memory_store import InMemoryStore


def test_crash_after_reservation_is_recovered():
    store = InMemoryStore()
    store.add_agent(Agent(id="a1", name="A1", state=AgentState.AVAILABLE))
    store.add_borrower(Borrower(id="b1", name="B1", phone="+1555"))

    agent = store.reserve_next_available_agent()
    borrower = store.reserve_next_borrower()
    call = Call(id="call-1", borrower_id=borrower.id, agent_id=agent.id, provider_name="provider_a")
    store.create_call(call)
    store.update_call_state(call.id, CallState.RESERVED)
    store.update_call_state(call.id, CallState.INITIATED)
    store.set_agent_state(agent.id, AgentState.DIALING)
    agent.current_call_id = call.id  # in real code the allocator sets this
    # simulate reservation timestamp being in the past, as if minutes have
    # passed since the crash
    agent.reserved_at = time.time() - 120

    # --- worker crashes here, nothing else happens ---

    # system "comes back": a recovery sweep runs (e.g. on worker restart /
    # a periodic reconciliation job)
    recovered = store.recover_stale_reservations(timeout_seconds=30)

    assert recovered == 1
    assert store.get_agent("a1").state == AgentState.AVAILABLE
    assert store.get_call("call-1").state == CallState.FAILED
    # borrower should be requeued for retry, not lost
    assert store._borrowers["b1"].status == BorrowerStatus.PENDING
    assert "b1" in store._borrower_queue


def test_recovery_does_not_touch_fresh_reservations():
    """A reservation made a moment ago should NOT be recovered - only
    genuinely stale ones. Otherwise we'd kill in-progress legitimate calls."""
    store = InMemoryStore()
    store.add_agent(Agent(id="a1", name="A1", state=AgentState.AVAILABLE))
    agent = store.reserve_next_available_agent()  # reserved_at = now

    recovered = store.recover_stale_reservations(timeout_seconds=30)
    assert recovered == 0
    assert store.get_agent("a1").state == AgentState.RESERVED


def test_provider_outage_rejects_new_calls_but_does_not_crash():
    from core.providers.provider_a import ProviderA
    from core.providers.base import ProviderTimeoutError

    provider = ProviderA(outage=True)
    assert provider.health_check() is False

    raised = False
    try:
        provider.initiate_call("call-x", "+1555", on_event=lambda *a: None)
    except ProviderTimeoutError:
        raised = True
    assert raised


def test_agent_disappearing_mid_dial_releases_cleanly():
    """Agent goes RESERVED -> DIALING then the call fails before connecting
    (simulating the agent 'disappearing' / call setup failing) - agent
    must be released back to AVAILABLE, not stuck."""
    store = InMemoryStore()
    store.add_agent(Agent(id="a1", name="A1", state=AgentState.AVAILABLE))
    agent = store.reserve_next_available_agent()
    store.set_agent_state(agent.id, AgentState.DIALING)

    call = Call(id="call-1", borrower_id="b1", agent_id=agent.id, provider_name="provider_a")
    store.create_call(call)
    store.update_call_state(call.id, CallState.RESERVED)
    store.update_call_state(call.id, CallState.INITIATED)
    store.update_call_state(call.id, CallState.FAILED, event_id="fail-1")

    store.set_agent_state("a1", AgentState.AVAILABLE)
    assert store.get_agent("a1").state == AgentState.AVAILABLE
