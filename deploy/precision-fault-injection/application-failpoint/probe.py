"""Minimal in-process failpoint registry used by the reviewed local profile."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Dict, Optional


@dataclass
class Activation:
    token: str
    failpoint_id: str
    request_id: str
    error_code: str
    remaining_hits: int
    expires_at_monotonic: float


class FailpointRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._activations: Dict[str, Activation] = {}
        self._clock = clock

    def enable(self, activation: Activation) -> None:
        if activation.remaining_hits < 1:
            raise ValueError("remaining_hits must be positive")
        self._activations[activation.token] = activation

    def disable(self, token: str) -> None:
        self._activations.pop(token, None)

    def evaluate(self, failpoint_id: str, request_id: str) -> Optional[str]:
        for activation in self._activations.values():
            if self._clock() >= activation.expires_at_monotonic:
                continue
            if (
                activation.failpoint_id == failpoint_id
                and activation.request_id == request_id
                and activation.remaining_hits > 0
            ):
                activation.remaining_hits -= 1
                return activation.error_code
        return None


def checkout_after_capture(registry: FailpointRegistry, request_id: str) -> str:
    error_code = registry.evaluate("checkout.payment.after_capture", request_id)
    return f"error:{error_code}" if error_code else "ok"
