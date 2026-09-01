"""
Provider B: slower, occasional timeouts, sends DUPLICATE events, and
sometimes delivers events OUT OF ORDER. This exists specifically to prove
the store's idempotency + state-machine validation actually work, not just
on paper.
"""

import threading
import time
import random
import uuid
from typing import Callable

from core.providers.base import TelecomProvider, ProviderTimeoutError, ProviderUnavailableError


class ProviderB(TelecomProvider):
    name = "provider_b"

    def __init__(self, answer_rate: float = 0.5, avg_talk_time: float = 90.0,
                 timeout_rate: float = 0.10, duplicate_rate: float = 0.25,
                 out_of_order_rate: float = 0.15, outage: bool = False):
        self.answer_rate = answer_rate
        self.avg_talk_time = avg_talk_time
        self.timeout_rate = timeout_rate
        self.duplicate_rate = duplicate_rate
        self.out_of_order_rate = out_of_order_rate
        self.outage = outage

    def health_check(self) -> bool:
        return not self.outage

    def initiate_call(self, call_id: str, phone_number: str,
                       on_event: Callable[[str, str, str], None]) -> str:
        if self.outage:
            raise ProviderUnavailableError(f"[provider_b] outage - call {call_id} rejected")

        if random.random() < self.timeout_rate:
            raise ProviderTimeoutError(f"[provider_b] setup timeout for call {call_id}")

        provider_call_id = f"pb-{uuid.uuid4().hex[:8]}"
        t = threading.Thread(target=self._simulate_call, args=(call_id, on_event), daemon=True)
        t.start()
        return provider_call_id

    def _emit(self, call_id, event_type, on_event, event_id=None):
        event_id = event_id or str(uuid.uuid4())
        on_event(call_id, event_type, event_id)
        # randomly re-deliver the same event again (duplicate)
        if random.random() < self.duplicate_rate:
            time.sleep(random.uniform(0.01, 0.1))
            on_event(call_id, event_type, event_id)  # same event_id -> store dedupes it
        return event_id

    def _simulate_call(self, call_id: str, on_event: Callable[[str, str, str], None]):
        time.sleep(random.uniform(0.2, 0.6))  # slower setup than Provider A
        events = []

        events.append(("INITIATED", None))
        events.append(("RINGING", None))

        if random.random() < 0.08:
            events.append(("FAILED", None))
        else:
            if random.random() <= self.answer_rate:
                events.append(("ANSWERED", None))
                events.append(("CONNECTED", None))
                events.append(("COMPLETED", None))
            else:
                events.append(("FAILED", None))

        # occasionally shuffle the tail of the event list to simulate
        # out-of-order delivery (e.g. COMPLETED arriving before ANSWERED)
        if len(events) > 2 and random.random() < self.out_of_order_rate:
            tail = events[1:]
            random.shuffle(tail)
            events = [events[0]] + tail

        for event_type, _ in events:
            time.sleep(random.uniform(0.1, 0.4))
            self._emit(call_id, event_type, on_event)
