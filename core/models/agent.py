"""
Agent model and state machine.

States (per assignment spec):
    OFFLINE -> AVAILABLE -> RESERVED -> DIALING -> CONNECTED -> WRAP_UP -> AVAILABLE
                                |                                            |
                                v                                            v
                            (release on failure)                      PAUSED / OFFLINE

Only transitions listed in ALLOWED_TRANSITIONS are permitted. Anything else
is rejected by the store, which is what keeps the system in a "sensible and
consistent state" even under weird/duplicate/out-of-order provider events.
"""

from dataclasses import dataclass, field
from enum import Enum
import time


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


# from_state -> set of valid to_states
ALLOWED_TRANSITIONS = {
    AgentState.OFFLINE: {AgentState.AVAILABLE},
    AgentState.AVAILABLE: {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.RESERVED: {AgentState.DIALING, AgentState.AVAILABLE},  # AVAILABLE = release on failed setup
    AgentState.DIALING: {AgentState.CONNECTED, AgentState.AVAILABLE},  # AVAILABLE = call failed to connect
    AgentState.CONNECTED: {AgentState.WRAP_UP},
    AgentState.WRAP_UP: {AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.PAUSED: {AgentState.AVAILABLE, AgentState.OFFLINE},
}


def is_transition_allowed(from_state: AgentState, to_state: AgentState) -> bool:
    if from_state == to_state:
        return True  # idempotent no-op, not an error
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


@dataclass
class Agent:
    id: str
    name: str
    state: AgentState = AgentState.OFFLINE
    current_call_id: str | None = None
    # version increments on every state change - used for optimistic
    # concurrency checks / debugging "which write won" questions.
    version: int = 0
    reserved_at: float | None = None
    updated_at: float = field(default_factory=time.time)
