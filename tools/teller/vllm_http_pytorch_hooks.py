"""Optional AgenticSRE TELLER hooks for vLLM request-level traces.

This module complements TELLER's CUPTI/NVTX collection. It writes lightweight
JSONL span events into the same trace directory so AgenticSRE can reconstruct:

HTTP ingress -> vLLM engine -> PyTorch frontend -> CUDA Runtime/Driver -> GPU Kernel

Usage in a vLLM container:
    export AGENTICSRE_TELLER_HOOKS=1
    export TELLER_DATA_DIR=/opt/teller/data
    export PYTHONPATH=/opt/agenticsre/tools/teller:$PYTHONPATH
    python -c "import vllm_http_pytorch_hooks; vllm_http_pytorch_hooks.install(); ..."

Set AGENTICSRE_TELLER_PYTORCH_HOOK=1 to enable the PyTorch module hook. The
PyTorch hook can be verbose, so keep it sampled or filtered in production.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional


_INSTALLED = False
_HEARTBEAT_STARTED = False
_WRITE_LOCK = threading.Lock()
_CURRENT_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agenticsre_teller_request_id",
    default="",
)


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _trace_dir() -> Path:
    configured = (
        os.environ.get("AGENTICSRE_TELLER_TRACE_DIR")
        or os.environ.get("TELLER_TRACE_DIR")
        or os.environ.get("TRUFFLD_DATA_DIR")
    )
    if configured:
        return Path(configured)
    data_dir = os.environ.get("TELLER_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "trace"
    return Path("/tmp/agenticsre-teller-trace")


def _span_path(kind: str) -> Path:
    return _trace_dir() / f"agenticsre_{kind}_spans_pid{os.getpid()}.jsonl"


def _write_event(kind: str, event: dict[str, Any]) -> None:
    try:
        path = _span_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        event.setdefault("pid", os.getpid())
        event.setdefault("tid", threading.get_ident())
        event.setdefault("source", "agenticsre_teller_hook")
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # These hooks must never break inference traffic.
        return


def _install_heartbeat() -> bool:
    """Write lightweight liveness events so idle services are not misread as stopped."""
    global _HEARTBEAT_STARTED
    if _HEARTBEAT_STARTED:
        return True
    interval = max(5, int(os.environ.get("AGENTICSRE_TELLER_HEARTBEAT_INTERVAL", "30") or "30"))
    _HEARTBEAT_STARTED = True

    def loop() -> None:
        while True:
            _write_event("heartbeat", {
                "type": "HEARTBEAT",
                "name": "agenticsre_teller_collector_alive",
                "start": time.time_ns(),
                "duration": 0,
                "interval_seconds": interval,
            })
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="agenticsre-teller-heartbeat", daemon=True)
    thread.start()
    return True


def _request_id_from_headers(headers: Any) -> str:
    for key in (
        "x-request-id",
        "x-correlation-id",
        "x-openai-request-id",
        "x-agenticsre-request-id",
    ):
        try:
            value = headers.get(key)
        except Exception:
            value = None
        if value:
            return str(value)
    return "http-" + uuid.uuid4().hex[:16]


def _install_fastapi_http_hook() -> bool:
    try:
        import fastapi  # type: ignore
    except Exception:
        return False

    original_init = getattr(fastapi.FastAPI, "__init__", None)
    if original_init is None or getattr(original_init, "_agenticsre_teller_wrapped", False):
        return False

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)

        @self.middleware("http")
        async def agenticsre_teller_http_span(request: Any, call_next: Callable[[Any], Any]) -> Any:
            request_id = _request_id_from_headers(getattr(request, "headers", {}))
            token = _CURRENT_REQUEST_ID.set(request_id)
            start_ns = time.time_ns()
            status_code: Optional[int] = None
            try:
                response = await call_next(request)
                status_code = getattr(response, "status_code", None)
                try:
                    response.headers["x-agenticsre-request-id"] = request_id
                except Exception:
                    pass
                return response
            finally:
                duration_ns = max(0, time.time_ns() - start_ns)
                _CURRENT_REQUEST_ID.reset(token)
                route = getattr(getattr(request, "scope", {}) or {}, "get", lambda *_: None)("path")
                method = getattr(request, "method", "") or ""
                _write_event("http", {
                    "type": "HTTP",
                    "name": f"{method} {route or ''}".strip() or "http_request",
                    "start": start_ns,
                    "duration": duration_ns,
                    "request_id": request_id,
                    "method": method,
                    "path": route or "",
                    "route": route or "",
                    "status_code": status_code,
                })

    patched_init._agenticsre_teller_wrapped = True  # type: ignore[attr-defined]
    fastapi.FastAPI.__init__ = patched_init
    return True


def _module_name(module: Any) -> str:
    cls = module.__class__
    return f"{cls.__module__}.{cls.__name__}"


def _should_record_module(name: str) -> bool:
    pattern = os.environ.get(
        "AGENTICSRE_TELLER_PYTORCH_FILTER",
        r"(vllm|transformers|attention|decoder|model|lm_head|embed|mlp|norm)",
    )
    try:
        return bool(re.search(pattern, name, re.IGNORECASE))
    except re.error:
        return True


def _install_pytorch_hook() -> bool:
    if not _truthy(os.environ.get("AGENTICSRE_TELLER_PYTORCH_HOOK", "")):
        return False
    try:
        import torch  # type: ignore
    except Exception:
        return False

    original_call = torch.nn.Module._call_impl
    if getattr(original_call, "_agenticsre_teller_wrapped", False):
        return False

    sample_rate = max(1, int(os.environ.get("AGENTICSRE_TELLER_PYTORCH_SAMPLE", "20") or "20"))
    counter = {"value": 0}

    def wrapped_call(self: Any, *args: Any, **kwargs: Any) -> Any:
        name = _module_name(self)
        counter["value"] += 1
        should_sample = counter["value"] % sample_rate == 0
        if not should_sample or not _should_record_module(name):
            return original_call(self, *args, **kwargs)
        request_id = _CURRENT_REQUEST_ID.get("")
        start_ns = time.time_ns()
        try:
            return original_call(self, *args, **kwargs)
        finally:
            event: dict[str, Any] = {
                "type": "PYTORCH",
                "name": name,
                "start": start_ns,
                "duration": max(0, time.time_ns() - start_ns),
            }
            if request_id:
                event["request_id"] = request_id
            _write_event("pytorch", event)

    wrapped_call._agenticsre_teller_wrapped = True  # type: ignore[attr-defined]
    torch.nn.Module._call_impl = wrapped_call
    return True


def install() -> dict[str, Any]:
    """Install optional HTTP/PyTorch hooks and return enabled capabilities."""
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True}
    _INSTALLED = True
    enabled = {
        "installed": True,
        "trace_dir": str(_trace_dir()),
        "heartbeat": _install_heartbeat(),
        "http": _install_fastapi_http_hook(),
        "pytorch": _install_pytorch_hook(),
    }
    _write_event("hook", {
        "type": "HOOK",
        "name": "agenticsre_teller_hooks.install",
        "start": time.time_ns(),
        "duration": 0,
        "enabled": enabled,
    })
    return enabled


if _truthy(os.environ.get("AGENTICSRE_TELLER_HOOKS", "")):
    install()
