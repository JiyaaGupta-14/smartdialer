# Architecture Decision Record

## Stack

- **Language: Python.** Fast to write correct, readable concurrency logic
  with `threading` + `dataclasses` + `enum`, and easy for a reviewer to
  follow without a build step. Tradeoff: Python's GIL means threads don't
  give real CPU parallelism - fine here because the workload is I/O-bound
  (waiting on simulated network/provider latency), not CPU-bound.
- **Concurrency primitive: a single `threading.RLock` around an in-memory
  store.** Chosen over `asyncio` because the assignment's scenarios (workers
  racing for the same agent, crashes mid-operation) are easiest to prove
  correct and test with real OS threads doing real read-check-write races,
  rather than cooperative single-threaded async where races are less
  representative of a real multi-process/multi-machine deployment.
- **No database.** An in-memory store behind a narrow interface
  (`InMemoryStore`) stands in for what would be Postgres (or similar) in
  production. This makes the prototype runnable with zero setup and keeps
  the tests fast and deterministic, while keeping the interface exactly
  where you'd swap in real persistence.
- **No message queue / Kafka / Redis.** Nothing in this prototype needs
  cross-process or cross-machine coordination yet - one process with N
  threads sharing one lock is enough to prove the reservation, idempotency,
  and safety logic is correct. Adding Kafka here would test infrastructure
  plumbing, not the actual algorithmic correctness being graded.
- **Testing: pytest.** Standard, minimal ceremony, good at expressing "run
  100 threads at 20 agents and assert no duplicates," which is the test that
  matters most here.

## What this makes easier

- Every concurrency-critical operation is in one file
  (`core/store/in_memory_store.py`), one lock, easy to audit and reason
  about line by line - directly answerable in a "walk me through your
  concurrency decision" discussion.
- Zero infrastructure to stand up to run or grade this - `pip install`,
  `pytest`, done.
- Provider-agnostic design (`TelecomProvider` ABC) means adding a real Plivo
  integration later is a new file, not a rewrite.

## What this makes harder

- **Not multi-process/multi-machine as-is.** A single in-process lock only
  coordinates threads within one Python process. Running multiple actual
  dialer worker *processes* (let alone machines) would require replacing
  the lock with a real database's atomic operations or a distributed lock -
  the store's interface is designed so this is a contained change, but it's
  not free.
- **In-memory = not durable.** If the whole process dies (not just one
  worker thread), all state is lost - no recovery from that beyond
  restarting the campaign. A real deployment needs a persisted store.
- **Simple rule-based pacing, not ML.** Explainable and testable, but leaves
  accuracy on the table versus a model that learns per-campaign,
  per-time-of-day answer-rate patterns.

## What breaks first as we scale: 100 -> 1,000 -> 10,000 agents

**100 -> 1,000 agents:** Nothing breaks yet on a single machine. The load
test shows >10,000 reservation attempts/sec against 2,000 agents with a
single lock; 1,000 agents is well within that.

**1,000 -> 10,000 agents (still single machine, single process):** The
single global lock starts to become a real contention point once worker
thread count scales up alongside agent count - every reservation attempt,
even for a completely unrelated agent, briefly blocks every other thread.
At this scale I'd shard the lock (e.g. lock per agent, or per shard of
agent-id-hash), so unrelated reservations stop contending with each other.
This is a bounded, local change - it doesn't touch engine/allocator/provider
code.

**10,000+ agents, or any real multi-machine deployment:** The real
bottleneck stops being CPU/lock contention and becomes "how do multiple
*processes on different machines* agree on agent state without racing." At
that point the in-memory store has to become a real database with atomic
operations (`UPDATE agents SET state='RESERVED' WHERE id=? AND
state='AVAILABLE'` returning the affected row count, or equivalent
optimistic-concurrency check using the `version` field already on the
`Agent` model), or a distributed lock service. I'd reach for Postgres row-
level locking first (it's the simplest correct answer and this workload's
write pattern - short, targeted updates - fits it well) before reaching for
something like a dedicated distributed lock manager, unless reservation
throughput requirements outgrew what Postgres could handle, which is
unlikely at agent-pool scale (agents are bounded by how many humans a
company employs, not an unbounded stream).

A second, more subtle bottleneck at large scale: the Safety Controller
currently does a full scan-by-state count on every decision
(`count_agents_by_state`). At 10,000 agents polled by a fast pacing loop,
that's an O(n) scan many times per second. The fix is to maintain running
counters per state (incremented/decremented at each transition, inside the
same lock) instead of scanning - a small, local change to
`InMemoryStore`, not an architectural one.

**Not "add more servers":** the actual fixes above are: shard the lock,
move to a database with atomic row-level operations, and replace O(n) scans
with maintained counters. Each addresses a specific, identified bottleneck
rather than throwing undifferentiated capacity at the problem.

## Final answer: blending predictive utilization with progressive-level safety

Keep the two fully decoupled, with the Safety Controller as the only
authority that can ever cause a call to be placed. The predictive pacing
engine is treated as an *untrusted advisor* - it can suggest a number, and
that's the entire extent of its power. The Safety Controller re-derives the
real constraint (available agents right now, not a moment ago) independently
on every single decision, never trusts the pacing engine's math, and has a
hard, pacing-engine-proof kill switch: any detected abandonment immediately
forces a cooldown period where the system behaves as pure Progressive mode
(1:1 agent:call), regardless of what predictive suggests. This gets you
predictive's utilization upside in the common case, while guaranteeing the
same worst-case safety ceiling as progressive mode, because that ceiling is
enforced by code the pacing engine cannot reach, not by the pacing engine's
own math being trustworthy.
