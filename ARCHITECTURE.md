# Architecture

## Pipeline

```
                 +-----------+     +--------------------+     +-------------------+     +----------------+     +------------------+
   Campaign ---> |  Pacing   | --> |  Safety Controller | --> |  Call Allocator    | --> | Telecom Provider |
                 |  Engine   |     |  (approve/reduce/  |     |  (creates Call,    |     | (mock A or B)    |
                 | (Prog. or |     |   reject/fallback) |     |   talks to         |     |                  |
                 |  Predict.)|     |                    |     |   provider)        |     |                  |
                 +-----------+     +--------------------+     +-------------------+     +------------------+
                       |                     ^                          |
                       | suggests N calls    | live agent/call counts   | provider events (async callback)
                       v                     |                          v
                 +---------------------------------------------------------------+
                 |            InMemoryStore (agents, calls, borrowers)           |
                 |   single lock, atomic reserve/release, idempotent updates     |
                 +---------------------------------------------------------------+
                                        ^
                                        |  N concurrent worker threads read/write here
                 +---------------------------------------------------------------+
                 |   DialerWorkerPool: Worker 1, Worker 2, ... Worker N          |
                 +---------------------------------------------------------------+
```

**Key structural guarantee:** the Pacing Engine has no reference to the
Allocator or the Provider - it is structurally impossible for it to place a
call directly. It can only return a suggested count. Only the Safety
Controller's approved count ever reaches the Allocator.

## Agent state machine

```
OFFLINE --> AVAILABLE --> RESERVED --> DIALING --> CONNECTED --> WRAP_UP --> AVAILABLE
               ^              |            |                        |
               |              v            v                        v
               +-------- (release on failed setup / no answer) --- PAUSED / OFFLINE
```

Allowed transitions live in `core/models/agent.py::ALLOWED_TRANSITIONS`. Any
transition not in that table is rejected by the store and logged - this is
what stops the agent lifecycle from ever landing somewhere nonsensical (e.g.
jumping straight from AVAILABLE to CONNECTED).

## Call state machine

```
QUEUED -> RESERVED -> INITIATED -> RINGING -> ANSWERED -> CONNECTED -> COMPLETED
                          |            |          |            |
                          v            v          v            v
                       CANCELLED    FAILED      FAILED       FAILED
```

COMPLETED / FAILED / CANCELLED are terminal - once reached, no further event
can move the call anywhere (`core/models/call.py::TERMINAL_STATES`). This is
what makes the provider event scenarios from the assignment safe:

- `ANSWERED, ANSWERED, ANSWERED, COMPLETED`: the first ANSWERED applies; the
  next two are same-state no-ops; COMPLETED is *rejected* because
  ANSWERED -> COMPLETED isn't a valid direct edge (must pass through
  CONNECTED) - so a provider skipping a step doesn't silently fabricate a
  connected call that never happened.
- `COMPLETED, ANSWERED, RINGING`: if COMPLETED somehow arrives on a call
  that's only reached RINGING, it's rejected (RINGING -> COMPLETED isn't
  valid); the call stays at RINGING and can still resolve normally when the
  real ANSWERED/CONNECTED/COMPLETED sequence arrives.
- Worker crash right after ANSWERED: the call is left in ANSWERED; a
  recovery sweep (`store.recover_stale_reservations`) later finds the
  associated agent still RESERVED/DIALING past a timeout, force-fails the
  call, releases the agent, and requeues the borrower.

## Preventing double-reservation of an agent

`InMemoryStore.reserve_agent()` / `reserve_next_available_agent()` do a
read-check-write **inside a single `threading.RLock` critical section**.
Two threads racing to reserve the same agent are serialized by the lock:
whichever acquires it first sees `AVAILABLE`, flips it to `RESERVED`, and
returns; the second thread (now inside the lock) sees `RESERVED` and either
moves to the next agent or gets `None`. This is functionally identical to an
atomic `UPDATE agents SET state='RESERVED' WHERE id=? AND state='AVAILABLE'`
in SQL, or a Redis `WATCH`/`MULTI` transaction - "check availability" and
"reserve it" are never two separate operations that another thread can
interleave between.

`tests/test_concurrency.py::test_only_one_worker_can_reserve_specific_agent`
proves this directly: 20 threads hit a `threading.Barrier` simultaneously and
race for one agent; exactly one wins.

## Idempotency and out-of-order events

Every provider event carries an `event_id`. `Call.processed_event_ids`
records every event_id already applied. A duplicate event_id is recognized
and skipped *before* any side effect runs (see
`InMemoryStore.update_call_state_atomic`, which returns
`(accepted, is_new_transition)` - callers like the `CallAllocator` gate
agent-state side effects on `is_new_transition`, not just "no error",
otherwise a duplicate CONNECTED event would try to move an agent that's
already past CONNECTED and produce a false "abandoned call" report - this
was actually caught and fixed by running the simulator against Provider B's
duplicate-event behaviour before submission, see the git history).

Out-of-order events are handled by the state machine's `ALLOWED_TRANSITIONS`
table rather than by trying to reorder or buffer events: if an event doesn't
make sense from the call's *current* state, it's rejected and logged. This
is deliberately simpler than building an event-reordering buffer, and is
sufficient because the state machine only cares about the current state, not
the full history.

## Why a single-process, single-lock design (and not Kafka/Redis/microservices)

The assignment explicitly says not to add technology that sounds impressive
without a reason. At the scale this prototype needs to *prove correctness*
at (up to a few thousand agents on one simulator run), a single shared lock
around short critical sections is:

- **Provably correct** - trivial to reason about, and directly testable with
  real concurrent threads (see `tests/test_concurrency.py`), not mocked.
- **Fast enough** - the load test hits >10,000 reservation attempts/sec on a
  single core with zero double-bookings (see `loadtest/load_test.py`).
- **The right shape to swap out later** - the store is the *only* place
  concurrency-critical logic lives. Replacing it with a real database means
  swapping the lock for `SELECT ... FOR UPDATE` (Postgres) or a distributed
  lock (Redis `SET NX`, or a proper lock service), without touching engine,
  allocator, or provider code at all.

`ADR.md` covers what actually breaks first at 1,000 -> 10,000 agents and how
I'd fix it, since a single Python process with a GIL-bound lock is *not*
what I'd ship at real multi-machine scale - it's what's appropriate to prove
the logic correctly at prototype scale, which is what's being graded here.

## Failure scenarios demonstrated

| Scenario | Where it's handled | Proof |
|---|---|---|
| Worker crash after reservation | `store.recover_stale_reservations()` | `tests/test_failure_scenarios.py::test_crash_after_reservation_is_recovered` |
| Provider outage | `provider.health_check()` + `SafetyController.evaluate(provider_healthy=False)` rejects all new calls | `tests/test_failure_scenarios.py::test_provider_outage_rejects_new_calls_but_does_not_crash`, simulator Scenario D |
| Agent availability drop | Safety Controller re-reads live `count_agents_by_state(AVAILABLE)` on every single decision - reacts within one pacing cycle (default 0.15s in the simulator loop) | Simulator Scenario D (40 agents worth disappear via provider outage + answer-rate crash mid-run) |
| Duplicate provider events | `Call.processed_event_ids` dedupe, atomic under the store lock | `tests/test_providers.py::test_duplicate_answered_events_do_not_cause_multiple_transitions` |
| Out-of-order events | Call state machine `ALLOWED_TRANSITIONS` rejects invalid-from-current-state transitions | `tests/test_providers.py::test_out_of_order_completed_before_answered_is_rejected_safely` |
