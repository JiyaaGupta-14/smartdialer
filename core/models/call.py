"""
Call model and state machine.

States (per assignment spec):
    QUEUED -> RESERVED -> INITIATED -> RINGING -> ANSWERED -> CONNECTED -> COMPLETED
                                             |          |            |
                                             v          v            v
                                          FAILED      FAILED       FAILED
    (any non-terminal state) -> CANCELLED

Terminal states: COMPLETED, FAILED, CANCELLED. Once a call reaches a
terminal state, no further transitions are accepted - this is what makes
out-of-order / duplicate provider events safe to ignore rather than
corrupt state.
"""

from dataclasses import dataclass, field
from enum import Enum
import time


class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}

ALLOWED_TRANSITIONS = {
    CallState.QUEUED: {CallState.RESERVED, CallState.CANCELLED},
    CallState.RESERVED: {CallState.INITIATED, CallState.CANCELLED, CallState.FAILED},
    CallState.INITIATED: {CallState.RINGING, CallState.FAILED, CallState.CANCELLED},
    CallState.RINGING: {CallState.ANSWERED, CallState.FAILED, CallState.CANCELLED},
    CallState.ANSWERED: {CallState.CONNECTED, CallState.FAILED},
    CallState.CONNECTED: {CallState.COMPLETED, CallState.FAILED},
    CallState.COMPLETED: set(),
    CallState.FAILED: set(),
    CallState.CANCELLED: set(),
}


def is_transition_allowed(from_state: CallState, to_state: CallState) -> bool:
    if from_state == to_state:
        return True  # duplicate event - idempotent no-op
    if from_state in TERMINAL_STATES:
        return False  # nothing can move a terminal call
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


@dataclass
class Call:
    id: str
    borrower_id: str
    agent_id: str | None
    provider_name: str
    state: CallState = CallState.QUEUED
    provider_call_id: str | None = None
    # every provider event we accept is recorded here (idempotency guard)
    processed_event_ids: set = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    connected_at: float | None = None
    ended_at: float | None = None
