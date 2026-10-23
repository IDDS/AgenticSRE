"""Host-native command builder for bare-metal vLLM fault injection.

This module is intentionally command-oriented: the AgenticSRE web backend can
render a safe dry-run command or execute the same command through SSH without
requiring the target GPU host to have this Python package installed.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Dict


@dataclass
class HostFaultCommand:
    fault_type: str
    layer: str
    supported: bool
    command: str
    recovery_command: str
    message: str


STATE_DIR = "/tmp/vllm_fault_injector"
VLLM_PID_CMD = (
    "ps -eo pid=,args= | "
    "awk '/vllm.entrypoints|vllm serve|api_server|openai.api_server/ "
    "&& $0 !~ /awk / && $0 !~ /pgrep / && $0 !~ /grep / "
    "&& $0 !~ /sh -c/ && $0 !~ /ssh / && $0 !~ /codex/ "
    "{print $1; exit}'"
)


def _py_cmd(code: str) -> str:
    return "python3 -c " + shlex.quote(code)


def _background(name: str, command: str) -> str:
    pidfile = f"{STATE_DIR}/{name}.pid"
    logfile = f"{STATE_DIR}/{name}.log"
    return (
        f"mkdir -p {shlex.quote(STATE_DIR)}; "
        f"nohup sh -c {shlex.quote(command)} > {shlex.quote(logfile)} 2>&1 & "
        f"echo $! > {shlex.quote(pidfile)}; "
        f"echo started {shlex.quote(name)} pid=$(cat {shlex.quote(pidfile)})"
    )


def _kill_pid(name: str) -> str:
    pidfile = f"{STATE_DIR}/{name}.pid"
    return (
        f"if [ -f {shlex.quote(pidfile)} ]; then "
        f"kill $(cat {shlex.quote(pidfile)}) 2>/dev/null || true; "
        f"rm -f {shlex.quote(pidfile)}; "
        f"fi"
    )


def _load_generator(endpoint: str, model: str, concurrent: int, rounds: int, prompt_len: int, max_tokens: int) -> str:
    code = f"""
import json
import threading
import urllib.request

endpoint = {endpoint!r}
model = {model!r}
payload = json.dumps({{"model": model, "prompt": "A" * {prompt_len}, "max_tokens": {max_tokens}}}).encode()
headers = {{"Content-Type": "application/json"}}

def worker():
    for _ in range({rounds}):
        try:
            req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
        except Exception:
            pass

threads = [threading.Thread(target=worker, daemon=True) for _ in range({concurrent})]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("request pressure completed")
""".strip()
    return _py_cmd(code)


def build_host_fault_command(
    fault_type: str,
    action: str = "inject",
    *,
    dry_run: bool = True,
    interface: str = "eth0",
    endpoint: str = "http://127.0.0.1:8000/v1/completions",
    model: str = "vllm",
    model_dir: str = "",
    concurrent: int = 16,
    rounds: int = 2,
    prompt_len: int = 2048,
    max_tokens: int = 128,
) -> HostFaultCommand:
    """Build a shell command for bare-metal host fault injection.

    Args:
        fault_type: Fault taxonomy name used by scenario YAML.
        action: "inject", "cleanup"/"recover", or "experiment".
        dry_run: If true, command is returned but not wrapped for execution.
        interface: Network interface for tc/iptables faults.
        endpoint: Local vLLM OpenAI-compatible completions endpoint.
        model: OpenAI-compatible model name used by business-side load requests.
        model_dir: Optional model directory for tokenizer/weight corruption.
        concurrent/rounds/prompt_len/max_tokens: Business-side load shape for
            request pressure scenarios.
    """
    iface = shlex.quote(interface)
    endpoint_q = shlex.quote(endpoint)
    model_root = model_dir or "/model /models /data/models"

    commands: Dict[str, HostFaultCommand] = {}

    process_crash_cmd = (
        f"pid=$({VLLM_PID_CMD}); "
        "if [ -n \"$pid\" ]; then kill -9 \"$pid\" && echo killed vLLM process $pid; "
        "else echo 'vLLM serving process not found' >&2; exit 2; fi"
    )
    commands["process_crash"] = HostFaultCommand(
        fault_type, "software", True, process_crash_cmd, "true",
        "Killed one matched vLLM serving process on the host.",
    )

    process_hang_cmd = (
        f"pid=$({VLLM_PID_CMD}); "
        "if [ -n \"$pid\" ]; then kill -STOP \"$pid\" && echo stopped vLLM process $pid; "
        "else echo 'vLLM serving process not found' >&2; exit 2; fi"
    )
    process_hang_recover = (
        f"pid=$({VLLM_PID_CMD}); "
        "if [ -n \"$pid\" ]; then kill -CONT \"$pid\" || true; fi"
    )
    commands["process_hang"] = HostFaultCommand(
        fault_type, "software", True, process_hang_cmd, process_hang_recover,
        "Paused one matched vLLM serving process with SIGSTOP.",
    )

    queue_cmd = _background(
        "request_queue_overflow",
        _load_generator(endpoint, model, concurrent=concurrent, rounds=rounds, prompt_len=prompt_len, max_tokens=max_tokens),
    )
    commands["request_queue_overflow"] = HostFaultCommand(
        fault_type, "software", True, queue_cmd, _kill_pid("request_queue_overflow"),
        "Started concurrent long-prompt load to overflow the vLLM request queue.",
    )

    batch_cmd = _background(
        "continuous_batch_disruption",
        _load_generator(endpoint, model, concurrent=concurrent, rounds=rounds, prompt_len=max(prompt_len, 4096), max_tokens=min(max_tokens, 128)),
    )
    commands["continuous_batch_disruption"] = HostFaultCommand(
        fault_type, "software", True, batch_cmd, _kill_pid("continuous_batch_disruption"),
        "Started mixed long-prefill load to degrade continuous batching.",
    )

    tokenizer_cmd = (
        f"set -e; for d in {model_root}; do "
        f"if [ -f \"$d/tokenizer_config.json\" ]; then "
        f"cp \"$d/tokenizer_config.json\" \"$d/tokenizer_config.json.bak.agenticsre\"; "
        f"printf '{{\"corrupt\": true}}\\n' > \"$d/tokenizer_config.json\"; "
        f"echo corrupted \"$d/tokenizer_config.json\"; exit 0; fi; done; "
        f"echo 'tokenizer_config.json not found' >&2; exit 2"
    )
    tokenizer_recover = (
        f"for d in {model_root}; do "
        f"if [ -f \"$d/tokenizer_config.json.bak.agenticsre\" ]; then "
        f"mv \"$d/tokenizer_config.json.bak.agenticsre\" \"$d/tokenizer_config.json\"; "
        f"echo restored \"$d/tokenizer_config.json\"; fi; done"
    )
    commands["tokenizer_error"] = HostFaultCommand(
        fault_type, "software", True, tokenizer_cmd, tokenizer_recover,
        "Corrupted tokenizer_config.json; restart vLLM if it does not reload model assets automatically.",
    )

    permission_cmd = (
        f"set -e; f=$(find {model_root} -name '*.safetensors' -o -name 'config.json' 2>/dev/null | head -1); "
        f"if [ -n \"$f\" ]; then chmod a-r \"$f\"; echo permission removed \"$f\"; "
        f"else echo 'model asset not found' >&2; exit 2; fi"
    )
    permission_recover = (
        f"find {model_root} -name '*.safetensors' -o -name 'config.json' 2>/dev/null | xargs -r chmod a+r"
    )
    commands["model_permission_error"] = HostFaultCommand(
        fault_type, "software", True, permission_cmd, permission_recover,
        "Removed read permission from one model asset.",
    )

    weight_cmd = (
        f"set -e; f=$(find {model_root} -name '*.safetensors' 2>/dev/null | head -1); "
        f"if [ -n \"$f\" ]; then cp \"$f\" \"$f.bak.agenticsre\"; "
        f"dd if=/dev/urandom of=\"$f\" bs=1 count=64 seek=1024 conv=notrunc 2>/dev/null; "
        f"echo corrupted \"$f\"; else echo 'safetensors shard not found' >&2; exit 2; fi"
    )
    weight_recover = (
        f"f=$(find {model_root} -name '*.safetensors.bak.agenticsre' 2>/dev/null | head -1); "
        f"if [ -n \"$f\" ]; then mv \"$f\" \"${{f%.bak.agenticsre}}\"; echo restored \"${{f%.bak.agenticsre}}\"; fi"
    )
    commands["weight_corruption"] = HostFaultCommand(
        fault_type, "software", True, weight_cmd, weight_recover,
        "Corrupted one safetensors shard; restore before restarting production service.",
    )

    pcie_code = """
import time
try:
    import torch
    h = torch.randn(1024, 1024, 64)
    deadline = time.time() + 120
    while time.time() < deadline:
        d = h.cuda()
        _ = d.cpu()
    print("PCIe pressure completed")
except Exception as e:
    print(f"PCIe pressure unavailable: {e}")
""".strip()
    commands["pcie_throttle"] = HostFaultCommand(
        fault_type, "driver", True, _background("pcie_throttle", _py_cmd(pcie_code)),
        _kill_pid("pcie_throttle"), "Started host-device copy pressure.",
    )

    commands["gpu_clock_throttle"] = HostFaultCommand(
        fault_type, "driver", True,
        "nvidia-smi -lgc 210,210 && echo GPU clocks locked",
        "nvidia-smi -rgc || true",
        "Locked GPU graphics clocks to a low range.",
    )

    cuda_code = """
import ctypes
try:
    cuda = ctypes.CDLL("libcuda.so")
    ctx = ctypes.c_void_p()
    cuda.cuCtxGetCurrent(ctypes.byref(ctx))
    if ctx.value:
        cuda.cuCtxDestroy(ctx)
    print("CUDA context corruption attempted in injector process")
except Exception as e:
    print(f"CUDA context corruption unavailable: {e}")
""".strip()
    commands["cuda_ctx_corruption"] = HostFaultCommand(
        fault_type, "driver", True, _py_cmd(cuda_code), "true",
        "Attempted CUDA context corruption in a local injector process.",
    )

    commands["packet_loss"] = HostFaultCommand(
        fault_type, "network", True,
        f"tc qdisc replace dev {iface} root netem loss 30% && echo packet loss injected on {iface}",
        f"tc qdisc del dev {iface} root 2>/dev/null || true",
        "Injected packet loss with tc/netem.",
    )
    commands["http_503"] = HostFaultCommand(
        fault_type, "network", True,
        "iptables -I INPUT -p tcp --dport 8000 -j REJECT && echo inference HTTP port rejected",
        "iptables -D INPUT -p tcp --dport 8000 -j REJECT 2>/dev/null || true",
        "Rejected the local OpenAI-compatible inference HTTP port.",
    )
    commands["bandwidth_throttle"] = HostFaultCommand(
        fault_type, "network", True,
        f"tc qdisc replace dev {iface} root tbf rate 10mbit burst 32kbit latency 400ms && echo bandwidth throttled on {iface}",
        f"tc qdisc del dev {iface} root 2>/dev/null || true",
        "Injected egress bandwidth limit.",
    )
    commands["rdma_failure"] = HostFaultCommand(
        fault_type, "network", True,
        "iptables -I OUTPUT -p tcp --dport 18515 -j DROP; "
        "iptables -I OUTPUT -p udp --dport 4791 -j DROP; "
        "echo RDMA/IB ports blocked",
        "iptables -D OUTPUT -p tcp --dport 18515 -j DROP 2>/dev/null || true; "
        "iptables -D OUTPUT -p udp --dport 4791 -j DROP 2>/dev/null || true",
        "Blocked common RDMA/IB ports.",
    )

    commands["disk_pressure"] = HostFaultCommand(
        fault_type, "os", True,
        _background("disk_pressure", "dd if=/dev/zero of=/tmp/agenticsre_disk_pressure bs=1M count=4096; sleep 300"),
        f"{_kill_pid('disk_pressure')}; rm -f /tmp/agenticsre_disk_pressure",
        "Started disk-space and write-pressure workload.",
    )

    fd_code = """
import os
import time
fds = []
try:
    while True:
        fds.append(os.open("/dev/null", os.O_RDONLY))
except OSError:
    print(f"opened {len(fds)} descriptors")
    time.sleep(300)
""".strip()
    commands["fd_exhaustion"] = HostFaultCommand(
        fault_type, "os", True, _background("fd_exhaustion", _py_cmd(fd_code)),
        _kill_pid("fd_exhaustion"), "Started file descriptor exhaustion process.",
    )

    cmd = commands.get(fault_type)
    if not cmd:
        return HostFaultCommand(
            fault_type, "unknown", False, "false", "true",
            f"Unsupported host-native fault type: {fault_type}",
        )

    normalized_action = "cleanup" if action == "recover" else action
    if normalized_action == "cleanup":
        executable = cmd.recovery_command
        message = f"Recovery command for {fault_type}"
    elif normalized_action == "experiment":
        executable = f"{cmd.command}; sleep 5; {cmd.recovery_command}"
        message = f"Experiment command for {fault_type}"
    else:
        executable = cmd.command
        message = cmd.message

    if dry_run:
        executable = f"printf %s\\\\n {shlex.quote(executable)}"

    return HostFaultCommand(
        cmd.fault_type,
        cmd.layer,
        cmd.supported,
        executable,
        cmd.recovery_command,
        message,
    )
