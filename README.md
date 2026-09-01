# SmartDialer

A functional prototype of a collections-campaign auto-dialer supporting both
**Progressive** and **Predictive** dialing modes, with a Safety Controller
that the predictive pacing engine cannot bypass, mock telecom providers with
different failure characteristics, and a test suite proving the concurrency
and correctness properties the assignment calls out.

## Architecture

```
Campaign > Pacing Engine (Progressive / Predictive) > Safety Controller > Call Allocator > Telecom Provider
```

See `ARCHITECTURE.md` for the diagram, state machines, and detailed reasoning,
and `ADR.md` for the short architecture-decision writeup (stack choices,
tradeoffs, what breaks at scale).

## Project layout

```
core/
  models/       Agent, Call, Borrower dataclasses + explicit state machines
  store/        Thread-safe in-memory store (the concurrency-critical piece)
  providers/    TelecomProvider interface + two mock providers (A: fast/reliable, B: slow/unreliable)
  engine/       ProgressiveDialer, PredictivePacingEngine, SafetyController
  allocator/    CallAllocator - the only component that talks to a provider
  workers/      DialerWorkerPool - simulates N concurrent dialer workers
  simulator/    Scenario runner (A/B/C/D from the assignment's table)
tests/          pytest suite - state machines, concurrency, safety, providers, failure scenarios
loadtest/       Basic concurrent load test / throughput benchmark
api/            FastAPI backend - exposes the core engine over HTTP for the dashboard
frontend/       Single-page dashboard (vanilla JS, no build step) - live campaign view,
                scenario runner, load test - served by api/server.py
```

## Dashboard

The dashboard is optional polish on top of the required CLI/test deliverables above,
but it's the easiest way to actually *see* the system behave:

- **Live Campaign tab**: pick Progressive or Predictive mode, a provider, agent/borrower
  counts, and watch the agent state machine update in real time (color-coded grid),
  along with a live feed of the Safety Controller's actual decision reasons (e.g.
  `"Reduced 6 -> 3 (available_agents=3, in_flight_ringing=0)"`) - this is the direct,
  visible answer to "why did the system decide to make this many calls right now."
- **Scenario Runner tab**: one click runs Scenario A/B/C (or D via the CLI simulator)
  in both modes and shows the same metrics table the CLI simulator produces.
- **Load Test tab**: run the concurrency proof from the browser and see "0 duplicate
  reservations" and throughput live.

No external or paid API is involved anywhere - the "API" is just the project's own
FastAPI server running on your machine (`127.0.0.1`), which the dashboard's JavaScript
calls directly.

## Setup

Requires Python 3.10+.

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

pip install -r requirements.txt
```

## Running things

**Tests:**
```bash
python -m pytest -v
python -m pytest --cov=core tests/       # with coverage
```

**Simulator** (runs scenarios A, B, C, D plus a Provider B chaos run and prints a metrics table):
```bash
python -m core.simulator.run_simulation
```

**Load test** (proves no double-booking under concurrent contention, reports throughput):
```bash
python -m loadtest.load_test 2000     # 2000 = number of agents to simulate
```

**Web dashboard** (live campaign view, scenario runner, load test - all in the browser):
```bash
uvicorn api.server:app --reload
```
Then open **http://127.0.0.1:8000** in a browser. Everything runs locally - no external
services, no API keys, nothing paid. The FastAPI backend serves both the REST API
and the dashboard HTML from one process on one port.

## What each mode does

**Progressive Mode** (`core/engine/progressive.py`): reserves one available
agent, reserves one borrower, asks the Safety Controller to approve exactly
1 call, places it if approved. Never creates more agent-bound calls than
there are available agents, because reservation is atomic and 1:1.

**Predictive Mode** (`core/engine/predictive.py`): a rule-based pacing
formula (`suggested_starts = ceil(available_agents / answer_rate) -
calls_ringing`, capped at a configurable multiplier) suggests how many calls
to start. It has no reference to the allocator or provider - it can only
return a number. The Safety Controller is the only thing that turns that
number into approved calls, and it independently re-checks live agent
availability, in-flight ringing calls, provider health, and a hard
kill-switch triggered by any detected abandonment.

## Answering "why did it start N calls?"

Both the pacing engine and the safety controller log a human-readable reason
string with every decision (`PacingEngine.suggest_call_count()` returns
`(count, reason)`; `SafetyController.evaluate()` returns a `SafetyDecision`
with a `.reason` field). Run the simulator and check the console output, or
inspect `safety.decision_log` / call `pacing.suggest_call_count()` directly.

## Concurrency model

A single shared, thread-safe `InMemoryStore` (one `threading.RLock`) stands
in for what would be a real database in a multi-machine deployment. All
agent/borrower reservation is atomic read-check-write under that lock, which
is what prevents two workers from reserving the same agent - see
`tests/test_concurrency.py` for a test that fires 100 threads at 20 agents
concurrently and asserts zero double-bookings. `ARCHITECTURE.md` explains why
this is sufficient for this prototype and what changes at real distributed
scale.

## Known limitations / what I'd do with another week

- The in-memory store is not persisted - a real crash of the whole process
  loses in-flight state. A real deployment would put Postgres (or similar)
  behind the same interface and use `SELECT ... FOR UPDATE` / optimistic
  version checks in place of the in-process lock.
- The predictive pacing formula is intentionally simple (no ML). A real
  system would fold in per-campaign historical data, time-of-day answer-rate
  curves, and confidence intervals on the answer-rate estimate.
- WRAP_UP is applied and immediately cleared in this simulation rather than
  timeboxed - a real system would hold agents in WRAP_UP for a configurable
  duration.
- No real telecom integration (mock providers only), per the assignment's
  "cherry on the cake" note.
