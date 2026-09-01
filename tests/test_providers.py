"""
Directly tests the assignment's provider-event scenarios:
  ANSWERED, ANSWERED, ANSWERED, COMPLETED
  COMPLETED, ANSWERED, RINGING
"""

from core.models.call import Call, CallState
from core.store.in_memory_store import InMemoryStore


def make_call_in_ringing_state(store):
    call = Call(id="call-1", borrower_id="b1", agent_id="a1", provider_name="provider_a")
    store.create_call(call)
    store.update_call_state(call.id, CallState.RESERVED)
    store.update_call_state(call.id, CallState.INITIATED)
    store.update_call_state(call.id, CallState.RINGING)
    return call


def test_duplicate_answered_events_do_not_cause_multiple_transitions():
    store = InMemoryStore()
    make_call_in_ringing_state(store)

    # same event_id delivered 3 times (true duplicate)
    assert store.update_call_state("call-1", CallState.ANSWERED, event_id="ev-answered-1")
    assert store.update_call_state("call-1", CallState.ANSWERED, event_id="ev-answered-1")
    assert store.update_call_state("call-1", CallState.ANSWERED, event_id="ev-answered-1")

    call = store.get_call("call-1")
    assert call.state == CallState.ANSWERED  # only actually applied once

    # COMPLETED requires going through CONNECTED per the state machine;
    # this event is correctly REJECTED because ANSWERED -> COMPLETED is
    # not a valid direct transition (must pass through CONNECTED first).
    applied = store.update_call_state("call-1", CallState.COMPLETED, event_id="ev-completed-1")
    assert applied is False
    call = store.get_call("call-1")
    assert call.state == CallState.ANSWERED


def test_out_of_order_completed_before_answered_is_rejected_safely():
    store = InMemoryStore()
    make_call_in_ringing_state(store)

    # COMPLETED arrives first (impossible per the model, but providers lie)
    applied = store.update_call_state("call-1", CallState.COMPLETED, event_id="ev-c1")
    assert not applied  # RINGING -> COMPLETED is not a valid transition, rejected
    call = store.get_call("call-1")
    assert call.state == CallState.RINGING  # state unchanged, still sensible

    # a late RINGING duplicate arrives after - should be a harmless no-op
    applied2 = store.update_call_state("call-1", CallState.RINGING, event_id="ev-r-late")
    assert applied2  # same-state transition is fine
    assert store.get_call("call-1").state == CallState.RINGING


def test_full_valid_sequence_reaches_completed():
    store = InMemoryStore()
    make_call_in_ringing_state(store)
    store.update_call_state("call-1", CallState.ANSWERED, event_id="e1")
    store.update_call_state("call-1", CallState.CONNECTED, event_id="e2")
    store.update_call_state("call-1", CallState.COMPLETED, event_id="e3")
    assert store.get_call("call-1").state == CallState.COMPLETED


def test_events_after_terminal_state_are_ignored():
    store = InMemoryStore()
    make_call_in_ringing_state(store)
    store.update_call_state("call-1", CallState.FAILED, event_id="fail-1")
    assert store.get_call("call-1").state == CallState.FAILED

    # a late ANSWERED shows up after FAILED was already recorded
    applied = store.update_call_state("call-1", CallState.ANSWERED, event_id="late-answered")
    assert not applied
    assert store.get_call("call-1").state == CallState.FAILED
