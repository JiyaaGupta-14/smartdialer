from core.models.call import CallState, is_transition_allowed, TERMINAL_STATES


def test_normal_call_lifecycle():
    assert is_transition_allowed(CallState.QUEUED, CallState.RESERVED)
    assert is_transition_allowed(CallState.RESERVED, CallState.INITIATED)
    assert is_transition_allowed(CallState.INITIATED, CallState.RINGING)
    assert is_transition_allowed(CallState.RINGING, CallState.ANSWERED)
    assert is_transition_allowed(CallState.ANSWERED, CallState.CONNECTED)
    assert is_transition_allowed(CallState.CONNECTED, CallState.COMPLETED)


def test_terminal_states_reject_everything():
    for terminal in TERMINAL_STATES:
        for target in CallState:
            if target != terminal:
                assert not is_transition_allowed(terminal, target), \
                    f"{terminal} should reject transition to {target}"


def test_cannot_skip_backwards():
    # e.g. COMPLETED arriving, then a late ANSWERED shows up
    assert not is_transition_allowed(CallState.COMPLETED, CallState.ANSWERED)
    assert not is_transition_allowed(CallState.COMPLETED, CallState.RINGING)


def test_duplicate_same_state_is_noop():
    assert is_transition_allowed(CallState.ANSWERED, CallState.ANSWERED)


def test_failure_reachable_from_most_states():
    assert is_transition_allowed(CallState.RESERVED, CallState.FAILED)
    assert is_transition_allowed(CallState.INITIATED, CallState.FAILED)
    assert is_transition_allowed(CallState.RINGING, CallState.FAILED)
    assert is_transition_allowed(CallState.ANSWERED, CallState.FAILED)
    assert is_transition_allowed(CallState.CONNECTED, CallState.FAILED)
