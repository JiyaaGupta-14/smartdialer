"""
Provider A: fast, reliable, low failure rate. Events always arrive in order,
never duplicated. This is the "well-behaved" baseline provider.
"""

import threading
import time
import random
import uuid
from typing import Callable

from core.providers.base import TelecomProvider, ProviderTimeoutError


class ProviderA(TelecomProvider):
    name = "provider_a"

    def __init__(self, answer_rate: float = 0.5, avg_talk_time: float = 90.0,
                 failure_rate: float = 0.03, outage: bool = False):
        self.answer_rate = answer_rate
        self.avg_talk_time = avg_talk_time
        self.failure_rate = failure_rate
        self.outage = outage  # can be flipped mid-simulation to test outage handling

    def health_check(self) -> bool:
        return not self.outage

    def initiate_call(self, call_id: str, phone_number: str,
                       on_event: Callable[[str, str, str], None]) -> str:
        if self.outage:
            raise ProviderTimeoutError(f"[provider_a] outage - call {call_id} rejected")

        provider_call_id = f"pa-{uuid.uuid4().hex[:8]}"
        t = threading.Thread(target=self._simulate_call, args=(call_id, on_event), daemon=True)
        t.start()
        return provider_call_id

    def _simulate_call(self, call_id: str, on_event: Callable[[str, str, str], None]):
        time.sleep(random.uniform(0.05, 0.2))  # fast setup
        on_event(call_id, "INITIATED", str(uuid.uuid4()))

        time.sleep(random.uniform(0.1, 0.3))
        on_event(call_id, "RINGING", str(uuid.uuid4()))

        if random.random() < self.failure_rate:
            time.sleep(0.1)
            on_event(call_id, "FAILED", str(uuid.uuid4()))
            return

        time.sleep(random.uniform(0.3, 1.0))
        if random.random() > self.answer_rate:
            on_event(call_id, "FAILED", str(uuid.uuid4()))  # no answer
            return

        on_event(call_id, "ANSWERED", str(uuid.uuid4()))
        on_event(call_id, "CONNECTED", str(uuid.uuid4()))

        talk_time = max(5.0, random.gauss(self.avg_talk_time, self.avg_talk_time * 0.2))
        time.sleep(min(talk_time, 2.0))  # compressed for simulation speed
        on_event(call_id, "COMPLETED", str(uuid.uuid4()))
