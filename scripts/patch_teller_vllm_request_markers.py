#!/usr/bin/env python3
"""Patch TELLER's vLLM hook to write request-level sidecar markers.

The patch is intentionally narrow and idempotent. It keeps NVTX emission in
place, and additionally writes NVTX_MARKER JSONL records into the active
TELLER_TRACE_DIR. AgenticSRE already parses this event shape, so request
context remains visible even when the CUPTI activity exporter records CUDA
RUNTIME/KERNEL events but drops NVTX marker text.
"""

from __future__ import annotations

import argparse
from pathlib import Path


HELPER = r'''
def _teller_request_marker_dir() -> str:
    trace_dir = os.environ.get("TELLER_TRACE_DIR")
    if trace_dir:
        return trace_dir
    data_dir = os.environ.get("TELLER_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "trace")
    return "/data/trace"


def _teller_write_request_marker(name: str, marker_id: int) -> None:
    try:
        trace_dir = _teller_request_marker_dir()
        os.makedirs(trace_dir, exist_ok=True)
        path = os.path.join(trace_dir, f"request_markers_pid{os.getpid()}.jsonl")
        ts = time.time_ns()
        pid = os.getpid()
        tid = threading.get_ident()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": "NVTX_MARKER",
                "n": name,
                "ts": ts,
                "id": marker_id,
                "pid": pid,
                "tid": tid,
                "source": "teller_python_sidecar",
            }, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warn(f"request marker write failed: {exc}")


def _teller_write_stage_span(event_type: str, name: str, start_ns: int, request_ids: str) -> None:
    try:
        trace_dir = _teller_request_marker_dir()
        os.makedirs(trace_dir, exist_ok=True)
        path = os.path.join(trace_dir, f"agenticsre_{event_type.lower()}_spans_pid{os.getpid()}.jsonl")
        duration_ns = max(0, time.time_ns() - int(start_ns or time.time_ns()))
        payload = {
            "type": event_type,
            "name": name,
            "start": start_ns,
            "duration": duration_ns,
            "request_ids": request_ids,
            "pid": os.getpid(),
            "tid": threading.get_ident(),
            "source": "agenticsre_teller_request_marker_patch",
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warn(f"stage span write failed: {exc}")


def _teller_marker_id(message: str) -> int:
    return abs(hash((message, os.getpid(), threading.get_ident()))) % 2147483647


def _teller_push_request_marker(message: str) -> int:
    marker_id = _teller_marker_id(message)
    _teller_write_request_marker(message, marker_id)
    return marker_id


def _teller_pop_request_marker(marker_id: int) -> None:
    _teller_write_request_marker("[range_end]", marker_id)


def _teller_extract_request_ids(execute_model_req: Any) -> str:
    if execute_model_req is None:
        return "unknown"
    seq_groups = getattr(execute_model_req, "seq_group_metadata_list", None)
    if seq_groups is None and isinstance(execute_model_req, (list, tuple)):
        seq_groups = execute_model_req
    request_ids = []
    for seq_group_metadata in seq_groups or []:
        request_id = getattr(seq_group_metadata, "request_id", None)
        if request_id:
            request_ids.append(str(request_id))
    return "_".join(request_ids) if request_ids else "unknown"

'''


def patch_impl(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text
    if "import threading" not in text:
        text = text.replace("import sys\n", "import sys\nimport threading\nimport time\n")
    if "def _teller_write_request_marker" not in text:
        text = text.replace("class TorchNVTXLoader(importlib.abc.Loader):\n", HELPER + "\nclass TorchNVTXLoader(importlib.abc.Loader):\n")
    elif "def _teller_write_stage_span" not in text:
        marker = "\ndef _teller_marker_id(message: str) -> int:\n"
        helper_stage = HELPER.split("\ndef _teller_marker_id(message: str) -> int:\n", 1)[0]
        stage_func = helper_stage.split("\ndef _teller_write_stage_span", 1)[1]
        text = text.replace(marker, "\ndef _teller_write_stage_span" + stage_func + marker)

    old = '''                    message = f"step_execute_model[request_ids:{request_ids}]"
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
'''
    new = '''                    message = f"step_execute_model[request_ids:{request_ids}]"
                    marker_id = _teller_push_request_marker(message)
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
                        _teller_pop_request_marker(marker_id)
'''
    text = text.replace(old, new)
    old_gpu_worker = '''                    request_ids = _extract_request_ids(execute_model_req)
                    nvtx.range_push(f"step_execute_model[request_ids:{request_ids}]")
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
'''
    new_gpu = '''                    request_ids = _teller_extract_request_ids(execute_model_req)
                    message = f"step_execute_model[request_ids:{request_ids}]"
                    marker_id = _teller_push_request_marker(message)
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
                        _teller_pop_request_marker(marker_id)
'''
    text = text.replace(old_gpu_worker, new_gpu)
    old_worker = '''                    request_ids = _extract_request_ids(execute_model_req)
                    nvtx.range_push(f"worker_execute_model[request_ids:{request_ids}]")
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
'''
    new_worker = '''                    request_ids = _teller_extract_request_ids(execute_model_req)
                    message = f"worker_execute_model[request_ids:{request_ids}]"
                    marker_id = _teller_push_request_marker(message)
                    stage_start_ns = time.time_ns()
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
                        _teller_pop_request_marker(marker_id)
                        _teller_write_stage_span("PYTORCH", "worker_execute_model", stage_start_ns, request_ids)
'''
    text = text.replace(old_worker, new_worker)
    patched_worker_without_span = '''                    request_ids = _teller_extract_request_ids(execute_model_req)
                    message = f"worker_execute_model[request_ids:{request_ids}]"
                    marker_id = _teller_push_request_marker(message)
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
                        _teller_pop_request_marker(marker_id)
'''
    if (
        "_teller_write_stage_span(\"PYTORCH\", \"worker_execute_model\"" not in text
        and patched_worker_without_span in text
    ):
        text = text.replace(patched_worker_without_span, new_worker)

    gpu_anchor = '''        elif self.fullname == "vllm.executor.gpu_executor":
'''
    if gpu_anchor not in text:
        insert = '''        elif self.fullname == "vllm.executor.gpu_executor":
            try:
                ExecutorBase = module.GPUExecutor
                original_execute_model = ExecutorBase.execute_model

                @functools.wraps(original_execute_model)
                def wrapped_step(self, *args, **kwargs):
                    nvtx = importlib.import_module("torch.cuda.nvtx")
                    execute_model_req = kwargs.get("execute_model_req") if kwargs else None
                    if execute_model_req is None and args:
                        execute_model_req = args[0]
                    request_ids = _teller_extract_request_ids(execute_model_req)
                    message = f"step_execute_model[request_ids:{request_ids}]"
                    marker_id = _teller_push_request_marker(message)
                    nvtx.range_push(message)
                    try:
                        return original_execute_model(self, *args, **kwargs)
                    finally:
                        nvtx.range_pop()
                        _teller_pop_request_marker(marker_id)
                ExecutorBase.execute_model = wrapped_step
                log.info("nvtx inject ok: GPUExecutor.execute_model")
            except Exception as e:
                log.error(f"nvtx inject fail (GPUExecutor.execute_model): {e}")
'''
        text = text.replace('        elif self.fullname == "vllm.executor.ray_distributed_executor":\n', insert + '        elif self.fullname == "vllm.executor.ray_distributed_executor":\n')
        text = text.replace('    "vllm.executor.ray_distributed_executor",\n', '    "vllm.executor.ray_distributed_executor",\n    "vllm.executor.gpu_executor",\n')

    if text == original:
        return False
    backup = path.with_suffix(path.suffix + ".bak-agenticsre")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("teller_code", help="Path to TELLER_Code root or src directory")
    args = parser.parse_args()
    root = Path(args.teller_code).resolve()
    candidates = [
        root / "src" / "teller" / "hook" / "impl.py",
        root / "teller" / "hook" / "impl.py",
    ]
    impl = next((p for p in candidates if p.exists()), None)
    if impl is None:
        raise SystemExit(f"cannot find teller hook impl.py under {root}")
    changed = patch_impl(impl)
    print(("patched" if changed else "already patched") + f": {impl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
