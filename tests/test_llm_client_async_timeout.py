"""Regression tests for cancellation-safe blocking LLM calls."""

import asyncio
import time

import pytest

from tools.llm_client import LLMClient, run_sync_in_daemon_thread


def test_cancelled_daemon_call_does_not_delay_asyncio_shutdown() -> None:
    def slow_call() -> str:
        time.sleep(0.5)
        return "late"

    started = time.monotonic()

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                run_sync_in_daemon_thread(slow_call),
                timeout=0.02,
            )

    asyncio.run(run())
    assert time.monotonic() - started < 0.25


def test_llm_async_chat_uses_cancellation_safe_runner() -> None:
    client = object.__new__(LLMClient)

    def slow_chat(*_args, **_kwargs) -> str:
        time.sleep(0.5)
        return "late"

    client.chat = slow_chat
    started = time.monotonic()

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.async_chat([{"role": "user", "content": "test"}]),
                timeout=0.02,
            )

    asyncio.run(run())
    assert time.monotonic() - started < 0.25
