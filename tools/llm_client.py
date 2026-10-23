"""
AgenticSRE LLM Client
OpenAI-compatible LLM wrapper supporting chat and structured JSON output.
"""

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from functools import partial
from typing import Any, Dict, List, Optional

from openai import OpenAI

from configs.config_loader import get_config
from tools.llm_receipt import JSONCompletion, completion_metadata

logger = logging.getLogger(__name__)


class ModelCallError(RuntimeError):
    """A safe failure receipt; provider bodies and credentials stay private."""
    def __init__(self, code, status_code=None):
        self.code = code
        self.status_code = status_code
        super().__init__(f'RCA model call failed ({code})')


async def run_sync_in_daemon_thread(func, /, *args, **kwargs):
    """Run blocking work without retaining asyncio's default executor.

    Cancelling ``asyncio.to_thread`` does not stop its synchronous worker, and
    ``asyncio.run`` then waits for that worker while shutting down the default
    executor.  A timed-out LLM request could therefore keep the whole RCA API
    run alive well beyond the reported deadline.  A dedicated daemon thread
    lets the cancelled pipeline return immediately; any late result is safely
    discarded while the client still enforces its own network timeout.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    call = partial(func, *args, **kwargs)

    def settle(result=None, error=None):
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker():
        try:
            result = call()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(settle, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(settle, result, None)
            except RuntimeError:
                pass

    threading.Thread(
        target=worker,
        daemon=True,
        name="agenticsre-blocking-call",
    ).start()
    return await future


class LLMClient:
    """OpenAI-compatible LLM client with JSON mode support."""

    def __init__(self, config=None):
        cfg = config or get_config().llm
        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
        )
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        *,
        thinking: Optional[bool] = None,
        json_mode: bool = False,
        request_timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
        _receipt: Optional[dict] = None,
    ) -> str:
        """Send chat completion request and return text response."""
        try:
            # An explicit per-call override for bounded DeepSeek classification.
            # Default RCA calls retain the provider's configured thinking mode.
            options = {}
            if thinking is False and str(model or self.model).startswith("deepseek-v4"):
                options = {"extra_body": {"thinking": {"type": "disabled"}}}
                if json_mode:
                    options["response_format"] = {"type": "json_object"}
            client = self.client
            if request_timeout_s is not None or max_retries is not None:
                transport = {}
                if request_timeout_s is not None:
                    transport['timeout'] = request_timeout_s
                if max_retries is not None:
                    transport['max_retries'] = max_retries
                client = client.with_options(**transport)
            resp = client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
                **options,
            )
            if _receipt is not None:
                _receipt.update(completion_metadata(resp))
                choices = getattr(resp, 'choices', None)
                choices = choices if isinstance(choices, list) else []
                content = getattr(getattr(choices[0], 'message', None), 'content', None) if choices else None
                content = content if isinstance(content, str) else ""
            else:
                content = resp.choices[0].message.content or ""
            total = getattr(getattr(resp, 'usage', None), 'total_tokens', None)
            logger.debug("LLM response (%d chars, tokens: %s)", len(content), total if type(total) is int else '?')
            return content
        except Exception as e:
            logger.error("LLM chat failed: %s", type(e).__name__)
            raise

    def json_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        thinking: Optional[bool] = None,
        request_timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
        with_receipt: bool = False,
    ) -> Any:
        """Send chat completion and parse JSON response."""
        # Append JSON instruction
        system_msg = messages[0] if messages and messages[0]["role"] == "system" else None
        if system_msg:
            if "json" not in system_msg["content"].lower():
                messages = list(messages)
                messages[0] = {
                    "role": "system",
                    "content": system_msg["content"] + "\n\nRespond with valid JSON only."
                }

        options = {"thinking": thinking} if thinking is not None else {}
        if request_timeout_s is not None:
            options['request_timeout_s'] = request_timeout_s
        if max_retries is not None:
            options['max_retries'] = max_retries
        if thinking is False and str(self.model).startswith("deepseek-v4"):
            options["json_mode"] = True
        receipt = {}
        if with_receipt:
            # Private to this call, including any late daemon-thread completion.
            options['_receipt'] = receipt
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, **options)

        # Try to extract JSON from markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            inside = False
            for line in lines:
                if line.strip().startswith("```") and not inside:
                    inside = True
                    continue
                elif line.strip() == "```" and inside:
                    break
                elif inside:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM JSON response, returning raw text wrapper")
            data = {"raw_response": text, "parse_error": True}
        return JSONCompletion(data, receipt) if with_receipt else data

    def summarize(self, text: str, instruction: str = "Summarize the following") -> str:
        """Convenience method for text summarization."""
        return self.chat([
            {"role": "system", "content": "You are a concise technical summarizer."},
            {"role": "user", "content": f"{instruction}:\n\n{text}"}
        ])

    async def async_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        *,
        thinking: Optional[bool] = None,
    ) -> str:
        """Async version of chat with cancellation-safe blocking execution."""
        options = {"thinking": thinking} if thinking is not None else {}
        return await run_sync_in_daemon_thread(
            self.chat, messages, temperature, max_tokens, model, **options
        )

    async def async_json_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        thinking: Optional[bool] = None,
        request_timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
        with_receipt: bool = False,
    ) -> Any:
        """Async version of json_chat with cancellation-safe blocking execution."""
        options = {"thinking": thinking} if thinking is not None else {}
        if request_timeout_s is not None:
            options['request_timeout_s'] = request_timeout_s
        if max_retries is not None:
            options['max_retries'] = max_retries
        if with_receipt:
            options['with_receipt'] = True
        return await run_sync_in_daemon_thread(
            self.json_chat, messages, temperature, max_tokens, **options
        )


class DeadlineLLMClient(LLMClient):
    """Per-RCA wall-clock budget, shared by its reasoning and review agents.

    Timed-out worker results stay in their private Future; they cannot replace
    a later diagnosis. Native request timeouts and zero retries also bound the
    lifetime of outstanding HTTP calls. Other workflows retain LLMClient.
    """

    def __init__(self, config, *, total_s: float = 480, call_s: float = 90):
        super().__init__(config)
        self.deadline = time.monotonic() + max(0.01, total_s)
        self.call_timeout_s = max(0.01, call_s)
        self.timeout_count = 0
        self.incomplete_responses = []
        self.failed_calls = []
        self.failed_call_count = 0
        self.terminal_failure = None
        self.client = self.client.with_options(timeout=self.call_timeout_s, max_retries=0)

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def chat(self, *args, **kwargs) -> str:
        terminal = getattr(self, 'terminal_failure', None)
        if terminal:
            raise ModelCallError(terminal['code'], terminal.get('status_code'))
        timeout = min(self.call_timeout_s, self.remaining_s())
        if timeout <= 0:
            raise TimeoutError("RCA model time budget exhausted")
        future = Future()
        # RCA consumers must never treat a length-limited/refused response as
        # completed reasoning, even when its partial text looks plausible.
        receipt = kwargs.get('_receipt')
        if receipt is None:
            receipt = {}
            kwargs['_receipt'] = receipt

        def invoke():
            try:
                future.set_result(super(DeadlineLLMClient, self).chat(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(target=invoke, daemon=True, name="rca-budgeted-llm").start()
        try:
            answer = future.result(timeout=timeout)
        except FutureTimeout:
            self.timeout_count += 1
            raise TimeoutError(f"RCA model call exceeded {timeout:.1f}s") from None
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            status = status if type(status) is int and 100 <= status <= 599 else None
            terminal_codes = {401:'provider_authentication_failed',402:'provider_payment_required',
                              403:'provider_permission_denied',404:'provider_endpoint_unavailable'}
            code = terminal_codes.get(status, 'model_call_failed')
            failure = {'code':code,'status_code':status}
            self.failed_calls = [*getattr(self, 'failed_calls', []), failure][-16:]
            self.failed_call_count = getattr(self, 'failed_call_count', 0) + 1
            if status in terminal_codes:
                self.terminal_failure = dict(failure)
            # Never pass an SDK exception body to callers that may persist it.
            raise ModelCallError(code, status) from None
        status = receipt.get('completion_status', 'unknown')
        if status != 'complete' or not isinstance(answer, str) or not answer.strip():
            status = status if status != 'complete' else 'empty'
            if not hasattr(self, 'incomplete_responses'):
                self.incomplete_responses = []
            self.incomplete_responses.append(status)
            raise ValueError(f"RCA model response was not complete ({status})")
        return answer
