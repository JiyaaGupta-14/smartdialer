"""
These tests directly answer the assignment's central question:
'Two workers see the same available agent at almost the same time.
Both must not be able to reserve that agent. Explain how you prevent it.'
"""

import threading
from core.models.agent import Agent, AgentState
from core.models.borrower import Borrower
from core.store.in_memory_store import InMemoryStore


def test_only_one_worker_can_reserve_specific_agent():
    store = InMemoryStore()
    store.add_agent(Agent(id="a1", name="A1", state=AgentState.AVAILABLE))

    results = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # force max contention - all threads hit the lock at once
        results.append(store.reserve_agent("a1"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, "exactly one worker should win the reservation"
    assert results.count(False) == 19
    assert store.get_agent("a1").state == AgentState.RESERVED


def test_concurrent_pool_reservation_never_double_books():
    """20 agents, 100 concurrent reservation attempts - total successful
    reservations must never exceed the number of agents that started
    AVAILABLE, and no agent should be double-reserved."""
    store = InMemoryStore()
    for i in range(20):
        store.add_agent(Agent(id=f"a{i}", name=f"A{i}", state=AgentState.AVAILABLE))

    won_agents = []
    lock = threading.Lock()

    def worker():
        agent = store.reserve_next_available_agent()
        if agent:
            with lock:
                won_agents.append(agent.id)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(won_agents) == 20, "exactly 20 reservations should succeed (one per agent)"
    assert len(set(won_agents)) == 20, "no agent should have been reserved twice"


def test_concurrent_borrower_allocation_never_double_assigns():
    store = InMemoryStore()
    for i in range(15):
        store.add_borrower(Borrower(id=f"b{i}", name=f"B{i}", phone="+1555"))

    won = []
    lock = threading.Lock()

    def worker():
        b = store.reserve_next_borrower()
        if b:
            with lock:
                won.append(b.id)

    threads = [threading.Thread(target=worker) for _ in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(won) == 15
    assert len(set(won)) == 15, "no borrower should be handed to two workers"
