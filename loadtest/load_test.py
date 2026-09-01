"""
Basic load test: python -m loadtest.load_test [num_agents]

Spins up a large agent pool + many workers hammering the store
concurrently, and reports reservation throughput and whether any
double-booking occurred. This is meant to give you real numbers to cite
when answering 'what breaks first at 1,000 / 10,000 agents' - run it with
increasing num_agents and watch reservations/sec.
"""

import sys
import time
import threading
from core.models.agent import Agent, AgentState
from core.store.in_memory_store import InMemoryStore


def run_load_test(num_agents=1000, num_workers=50, attempts_per_worker=200):
    store = InMemoryStore()
    for i in range(num_agents):
        store.add_agent(Agent(id=f"a{i}", name=f"A{i}", state=AgentState.AVAILABLE))

    won = []
    lock = threading.Lock()

    def worker():
        local_wins = []
        for _ in range(attempts_per_worker):
            agent = store.reserve_next_available_agent()
            if agent:
                local_wins.append(agent.id)
        with lock:
            won.extend(local_wins)

    threads = [threading.Thread(target=worker) for _ in range(num_workers)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    duplicates = len(won) - len(set(won))
    print(f"Agents: {num_agents} | Workers: {num_workers} | "
          f"Attempts/worker: {attempts_per_worker}")
    print(f"Total reservation attempts: {num_workers * attempts_per_worker}")
    print(f"Successful reservations: {len(won)} (expected <= {num_agents})")
    print(f"Duplicate reservations (should always be 0): {duplicates}")
    print(f"Elapsed: {elapsed:.3f}s")
    if elapsed > 0:
        print(f"Reservation throughput: {(num_workers * attempts_per_worker) / elapsed:.0f} attempts/sec")
    print(f"Result: {'PASS - no double booking' if duplicates == 0 else 'FAIL - double booking detected!'}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    run_load_test(num_agents=n, num_workers=50, attempts_per_worker=200)
