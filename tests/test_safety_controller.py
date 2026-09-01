from core.models.agent import Agent, AgentState
from core.store.in_memory_store import InMemoryStore
from core.engine.safety_controller import SafetyController


def make_store_with_agents(n_available, n_other=0):
    store = InMemoryStore()
    for i in range(n_available):
        store.add_agent(Agent(id=f"a{i}", name=f"A{i}", state=AgentState.AVAILABLE))
    for i in range(n_other):
        store.add_agent(Agent(id=f"o{i}", name=f"O{i}", state=AgentState.CONNECTED))
    return store


def test_never_approves_more_than_available_agents():
    store = make_store_with_agents(5)
    safety = SafetyController(store)
    decision = safety.evaluate(requested_count=50)
    assert decision.approved_count <= 5


def test_approves_exact_count_when_within_capacity():
    store = make_store_with_agents(10)
    safety = SafetyController(store)
    decision = safety.evaluate(requested_count=3)
    assert decision.approved_count == 3


def test_rejects_all_when_provider_unhealthy():
    store = make_store_with_agents(10)
    safety = SafetyController(store)
    decision = safety.evaluate(requested_count=5, provider_healthy=False)
    assert decision.approved_count == 0


def test_abandoned_call_forces_progressive_fallback():
    store = make_store_with_agents(10)
    safety = SafetyController(store)
    # before any abandonment, normal approvals happen
    d1 = safety.evaluate(requested_count=8)
    assert not d1.forced_progressive

    safety.record_abandoned_call()

    d2 = safety.evaluate(requested_count=8)
    assert d2.forced_progressive is True
    assert d2.approved_count <= 10  # still bounded by available agents


def test_zero_available_agents_means_zero_approved():
    store = make_store_with_agents(0, n_other=5)
    safety = SafetyController(store)
    decision = safety.evaluate(requested_count=10)
    assert decision.approved_count == 0
