"""
Telecom provider interface. The dialer only ever talks to this interface -
it never knows whether it's Provider A, Provider B, or a real Plivo/Twilio
integration underneath. This is what lets us swap providers, add a third
one, or point at a real telecom API later without touching engine code.
"""

from abc import ABC, abstractmethod
from typing import Callable


class ProviderTimeoutError(Exception):
    pass


class ProviderUnavailableError(Exception):
    """Raised when the provider is having an outage - allocator/safety
    controller should back off and stop sending new calls to it."""


class TelecomProvider(ABC):
    name: str = "base"

    @abstractmethod
    def initiate_call(self, call_id: str, phone_number: str,
                       on_event: Callable[[str, str, str], None]) -> str:
        """
        Start a call. Returns a provider_call_id immediately (synchronous
        accept). Events (RINGING/ANSWERED/COMPLETED/FAILED, etc.) are
        delivered asynchronously via on_event(call_id, event_type, event_id).

        Must raise ProviderTimeoutError / ProviderUnavailableError on
        failure rather than silently swallowing errors, so the allocator
        can react (mark call FAILED, release agent, back off).
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Cheap check the pacing/safety layer can poll to decide whether
        to keep sending this provider traffic."""
        raise NotImplementedError
