from core.models.agent import Agent, AgentState, is_transition_allowed


def test_valid_transitions_allowed():
    assert is_transition_allowed(AgentState.OFFLINE, AgentState.AVAILABLE)
    assert is_transition_allowed(AgentState.AVAILABLE, AgentState.RESERVED)
    assert is_transition_allowed(AgentState.RESERVED, AgentState.DIALING)
    assert is_transition_allowed(AgentState.DIALING, AgentState.CONNECTED)
    assert is_transition_allowed(AgentState.CONNECTED, AgentState.WRAP_UP)
    assert is_transition_allowed(AgentState.WRAP_UP, AgentState.AVAILABLE)


def test_invalid_transitions_rejected():
    assert not is_transition_allowed(AgentState.OFFLINE, AgentState.CONNECTED)
    assert not is_transition_allowed(AgentState.AVAILABLE, AgentState.CONNECTED)
    assert not is_transition_allowed(AgentState.CONNECTED, AgentState.OFFLINE)


def test_same_state_is_idempotent_noop():
    assert is_transition_allowed(AgentState.AVAILABLE, AgentState.AVAILABLE)


def test_agent_defaults():
    a = Agent(id="a1", name="Test Agent")
    assert a.state == AgentState.OFFLINE
    assert a.version == 0
    assert a.current_call_id is None
