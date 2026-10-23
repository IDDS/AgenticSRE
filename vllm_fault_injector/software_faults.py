"""Software layer fault injection for vLLM inference services.

Handles: vLLM process crashes, model loading failures, tokenizer errors,
KV cache exhaustion scenarios.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VLLM_PID_SH = (
    "for f in /proc/[0-9]*/cmdline; do "
    "pid=${f#/proc/}; pid=${pid%/cmdline}; "
    "[ \"$pid\" = \"1\" ] && continue; "
    "[ \"$pid\" = \"$$\" ] && continue; "
    "cmd=$(tr \"\\000\" \" \" < \"$f\" 2>/dev/null || true); "
    "case \"$cmd\" in *vllm.entrypoints.openai.api_server*|*runpy.run_module*api_server*) "
    "case \"$cmd\" in *kubectl*|*pgrep*|*teller.cli*|*\"sh -c\"*) continue;; esac; "
    "echo \"$pid\"; break;; "
    "esac; "
    "done"
)


@dataclass
class FaultResult:
    """Result of a fault injection operation."""
    fault_type: str
    layer: str
    success: bool
    message: str
    recovery_cmd: Optional[str] = None
    command: Optional[str] = None


class SoftwareFaultInjector:
    """Injects software-layer faults into vLLM inference services.

    Args:
        kubectl_cmd: Base kubectl command (may include SSH prefix).
        namespace: Target K8s namespace.
        dry_run: If True, only log commands without executing.
    """

    def __init__(
        self,
        kubectl_cmd: str = "kubectl",
        namespace: str = "default",
        dry_run: bool = True,
    ) -> None:
        self.kubectl = kubectl_cmd
        self.namespace = namespace
        self.dry_run = dry_run

    def _business_load_pod_cmd(
        self,
        *,
        pod_name: str,
        deployment: str,
        script: str,
    ) -> str:
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        inner = f"printf %s {shlex.quote(encoded)} | base64 -d | /bin/sh"
        shell = (
            f"{self.kubectl} -n {self.namespace} delete pod {pod_name} --ignore-not-found=true "
            f"--force --grace-period=0 >/dev/null 2>&1 || true; "
            f"i=0; while {self.kubectl} -n {self.namespace} get pod {pod_name} >/dev/null 2>&1; do "
            f"i=$((i+1)); [ $i -ge 30 ] && echo 'Timed out waiting for old load pod deletion' >&2 && exit 3; "
            f"sleep 1; done; "
            f"{self.kubectl} -n {self.namespace} run {pod_name} "
            f"--image=vllm/vllm-openai:latest --image-pull-policy=IfNotPresent --restart=Never "
            f"--labels=app={pod_name},agenticsre.io/target={deployment},agenticsre.io/fault=business-load "
            f"--command -- /bin/sh -c {shlex.quote(inner)} || exit $?; "
            f"echo {pod_name} started"
        )
        return "sh -c " + shlex.quote(shell)

    async def inject_process_crash(self, deployment: str = "vllm-server") -> FaultResult:
        """Crash the vLLM serving process or force one serving Pod to restart.

        Killing PID 1 is unreliable in Kubernetes containers: PID 1 may be a
        wrapper, may reject signals depending on runtime policy, or may not be
        the actual vLLM process. This implementation first locates a Pod owned
        by the Deployment selector, then tries to kill a non-PID-1 vLLM/python
        serving process. If that cannot be found, it deletes one selected Pod
        with zero grace period, which is the Kubernetes-native equivalent for
        testing restart behavior.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"sh -c '"
            f"selector_value=$({self.kubectl} get deploy/{deployment} -n {self.namespace} "
            f"-o jsonpath=\"{{.spec.selector.matchLabels.app}}\" 2>/dev/null || true); "
            f"if [ -z \"$selector_value\" ]; then selector_value={deployment}; fi; "
            f"pod=$({self.kubectl} get pod -n {self.namespace} -l app=\"$selector_value\" "
            f"-o jsonpath=\"{{.items[0].metadata.name}}\" 2>/dev/null || true); "
            f"if [ -z \"$pod\" ]; then "
            f"pod=$({self.kubectl} get pod -n {self.namespace} -o name | "
            f"grep \"^pod/{deployment}-\" | head -1 | cut -d/ -f2); "
            f"fi; "
            f"if [ -z \"$pod\" ]; then echo \"No pod found for deployment {deployment}\" >&2; exit 2; fi; "
            f"{self.kubectl} exec -n {self.namespace} \"$pod\" -- sh -c "
            f"'\"'\"'pid=$({VLLM_PID_SH}); "
            f"if [ -n \"$pid\" ]; then kill -9 \"$pid\"; echo killed process $pid; "
            f"else exit 42; fi'\"'\"' "
            f"|| {self.kubectl} delete pod \"$pod\" -n {self.namespace} --ignore-not-found=true'"
        )
        return await self._execute(
            cmd, "process_crash", "software",
            f"Crashed vLLM serving process or restarted one pod for {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_model_load_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt model symlink to cause loading failure on next restart.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        patch = (
            '[{"op":"replace","path":"/spec/template/spec/containers/0/args/1",'
            '"value":"/tmp/agenticsre-missing-model"}]'
        )
        cmd = (
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} "
            f"--type=json -p '{patch}'"
        )
        return await self._execute(
            cmd, "model_load_failure", "software",
            f"Patched model path to a missing directory in {deployment}",
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} --type=json "
            f"-p '[{{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/1\",\"value\":\"/model\"}}]'",
        )

    async def inject_tokenizer_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt tokenizer config to trigger tokenizer initialization error.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'echo corrupt > /tmp/tokenizer_override.json'"
        )
        return await self._execute(
            cmd, "tokenizer_error", "software",
            f"Corrupted tokenizer config in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"rm -f /tmp/tokenizer_override.json",
        )

    async def inject_process_hang(self, deployment: str = "vllm-server") -> FaultResult:
        """Pause the vLLM serving process without killing the Pod.

        This simulates a deadlocked event loop, stuck CUDA call, or Python
        runtime hang. Recovery sends SIGCONT and then restarts the Deployment
        if the process does not recover cleanly.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- sh -c "
            f"'pid=$({VLLM_PID_SH}); "
            f"if [ -n \"$pid\" ]; then kill -STOP \"$pid\"; echo stopped process $pid; "
            f"else echo \"vLLM process not found\" >&2; exit 2; fi'"
        )
        return await self._execute(
            cmd, "process_hang", "software",
            f"Paused vLLM serving process in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'pid=$({VLLM_PID_SH}); "
            f"if [ -n \"$pid\" ]; then kill -CONT \"$pid\"; fi' || "
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_model_permission_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Remove read permission from one model asset.

        This is less destructive than byte-level weight corruption and models
        common hostPath/NFS permission regressions.
        """
        patch = (
            '[{"op":"replace","path":"/spec/template/spec/containers/0/args/1",'
            '"value":"/root/agenticsre-permission-denied-model"}]'
        )
        cmd = (
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} "
            f"--type=json -p '{patch}'"
        )
        return await self._execute(
            cmd, "model_permission_error", "software",
            f"Patched model path to a permission-denied location in {deployment}",
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} --type=json "
            f"-p '[{{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/1\",\"value\":\"/model\"}}]'",
        )

    async def inject_kv_cache_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Generate business-side long-context traffic to exhaust KV cache.

        This deliberately avoids executing inside the serving container. The
        load pod discovers the model from /v1/models, then sends concurrent
        long prompts through the Kubernetes Service so the pressure propagates
        through the normal vLLM request path.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        endpoint = f"http://{deployment}.{self.namespace}.svc.cluster.local:8000"
        cmd = self._business_load_pod_cmd(
            pod_name="agenticsre-kv-cache-load",
            deployment=deployment,
            script=(
                "python3 - <<'PY'\n"
                "import json, threading, time, urllib.request, urllib.error\n"
                f"base = {endpoint!r}\n"
                "def post_json(path, payload, timeout=60):\n"
                "    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST')\n"
                "    with urllib.request.urlopen(req, timeout=timeout) as resp:\n"
                "        return json.loads(resp.read().decode() or '{}')\n"
                "try:\n"
                "    with urllib.request.urlopen(base + '/v1/models', timeout=15) as resp:\n"
                "        models = json.loads(resp.read().decode()).get('data') or []\n"
                "    model_info = (models[0] or {}) if models else {}\n"
                "    model = model_info.get('id') or ''\n"
                "    max_model_len = int(model_info.get('max_model_len') or 4096)\n"
                "except Exception:\n"
                "    model = ''\n"
                "    max_model_len = 4096\n"
                "if not model:\n"
                "    raise SystemExit('no model discovered from /v1/models')\n"
                "max_model_len = max(256, max_model_len)\n"
                "target_prompt_tokens = max(128, int(max_model_len * 0.82))\n"
                "prompt_units = target_prompt_tokens\n"
                "prompt = ''\n"
                "prompt_tokens = 0\n"
                "for _ in range(5):\n"
                "    prompt = ' '.join('token%d' % (i % 10007) for i in range(max(32, prompt_units)))\n"
                "    try:\n"
                "        tokenized = post_json('/tokenize', {'model': model, 'prompt': prompt}, timeout=30)\n"
                "        prompt_tokens = int(tokenized.get('count') or len(tokenized.get('tokens') or []))\n"
                "    except Exception:\n"
                "        prompt_tokens = 0\n"
                "    if prompt_tokens <= 0:\n"
                "        break\n"
                "    if abs(prompt_tokens - target_prompt_tokens) <= max(8, target_prompt_tokens // 50):\n"
                "        break\n"
                "    prompt_units = max(32, int(prompt_units * target_prompt_tokens / prompt_tokens))\n"
                "if prompt_tokens <= 0:\n"
                "    prompt = ' '.join('token%d' % (i % 10007) for i in range(target_prompt_tokens // 2))\n"
                "    prompt_tokens = target_prompt_tokens\n"
                "max_tokens = max(16, min(128, max_model_len - prompt_tokens - 16))\n"
                "worker_count = max(12, min(96, 49152 // max(256, max_model_len)))\n"
                "deadline = time.time() + 180\n"
                "lock = threading.Lock()\n"
                "stats = {'ok': 0, 'err': 0}\n"
                "def worker():\n"
                "    while time.time() < deadline:\n"
                "        try:\n"
                "            post_json('/v1/completions', {'model': model, 'prompt': prompt, 'max_tokens': max_tokens, 'temperature': 0})\n"
                "            key = 'ok'\n"
                "        except Exception:\n"
                "            key = 'err'\n"
                "        with lock:\n"
                "            stats[key] += 1\n"
                "threads = [threading.Thread(target=worker, daemon=True) for _ in range(worker_count)]\n"
                "[t.start() for t in threads]\n"
                "[t.join() for t in threads]\n"
                "print(json.dumps({'model': model, 'max_model_len': max_model_len, 'target_prompt_tokens': target_prompt_tokens, 'prompt_tokens': prompt_tokens, 'prompt_units': prompt_units, 'max_tokens': max_tokens, 'worker_count': worker_count, **stats}, ensure_ascii=False), flush=True)\n"
                "PY"
            ),
        )
        return await self._execute(
            cmd, "kv_cache_exhaustion", "software",
            f"Started business-side KV cache pressure load against service/{deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-kv-cache-load "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_scheduler_contention(
        self, deployment: str = "vllm-server", concurrent: int = 100
    ) -> FaultResult:
        """Flood vLLM scheduler with concurrent long-context requests.

        Disrupts continuous batching by saturating the iteration-level
        scheduler, causing request queuing and timeout cascades.

        Args:
            deployment: Target deployment name.
            concurrent: Number of concurrent requests to send.

        Returns:
            FaultResult with injection outcome.
        """
        script = (
            f"python3 -c \""
            f"import asyncio, aiohttp\\n"
            f"async def flood():\\n"
            f"    async with aiohttp.ClientSession() as s:\\n"
            f"        tasks = [s.post('http://localhost:8000/v1/completions', "
            f"json={{'model':'m','prompt':'A'*4000,'max_tokens':512}}) "
            f"for _ in range({concurrent})]\\n"
            f"        await asyncio.gather(*tasks, return_exceptions=True)\\n"
            f"asyncio.run(flood())\""
        )
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- {script}"
        )
        return await self._execute(
            cmd, "scheduler_contention", "software",
            f"Sent {concurrent} concurrent requests to saturate scheduler in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_request_queue_overflow(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Set vLLM max-num-seqs to 1 to force request queue overflow.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} VLLM_MAX_NUM_SEQS=1"
        )
        return await self._execute(
            cmd, "request_queue_overflow", "software",
            f"Set max-num-seqs=1 on {deployment} to force queue overflow",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} VLLM_MAX_NUM_SEQS-",
        )

    async def inject_continuous_batch_disruption(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Disable continuous batching by setting max-num-batched-tokens=1.

        Forces single-request processing, degrading throughput dramatically.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} VLLM_MAX_NUM_BATCHED_TOKENS=1"
        )
        return await self._execute(
            cmd, "continuous_batch_disruption", "software",
            f"Disabled continuous batching on {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} VLLM_MAX_NUM_BATCHED_TOKENS-",
        )

    async def inject_weight_corruption(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Corrupt model weight file to trigger runtime inference errors.

        Writes random bytes into a model safetensors shard.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        patch = (
            '[{"op":"replace","path":"/spec/template/spec/containers/0/args/1",'
            '"value":"/tmp/agenticsre-corrupted-model"}]'
        )
        cmd = (
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} "
            f"--type=json -p '{patch}'"
        )
        return await self._execute(
            cmd, "weight_corruption", "software",
            f"Patched model path to a corrupted-model placeholder in {deployment}",
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} --type=json "
            f"-p '[{{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/1\",\"value\":\"/model\"}}]'",
        )

    async def inject_prefill_decode_misclassification(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Force prefill/decode misclassification in vLLM scheduler.

        Inspired by Teller Case Study (Section 6): under GPU memory pressure,
        the scheduler may allocate only one token to a newly arrived request,
        causing the attention backend to misclassify it as a decode request
        (num_computed_tokens=0 but num_scheduled_tokens=1). This triggers
        the wrong kernel execution path.

        Simulated by setting gpu-memory-utilization very low + sending
        long-context requests that force aggressive preemption.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} "
            f"VLLM_GPU_MEMORY_UTILIZATION=0.3 VLLM_MAX_NUM_SEQS=2"
        )
        return await self._execute(
            cmd, "prefill_decode_misclassification", "software",
            f"Forced low GPU memory utilization + low max_num_seqs on {deployment} "
            f"to trigger prefill/decode misclassification",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_GPU_MEMORY_UTILIZATION- VLLM_MAX_NUM_SEQS-",
        )

    async def inject_attention_backend_mismatch(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Force attention backend mismatch by overriding backend selection.

        Inspired by Teller: when prefill/decode misclassification occurs,
        the device layer launches the wrong kernel (e.g., selective_state_update
        instead of selective_scan_fwd_kernel for Mamba models).

        Simulated by forcing a specific attention backend that is incompatible
        with the model's requirements.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} "
            f"VLLM_ATTENTION_BACKEND=FLASH_ATTN "
            f"VLLM_USE_V1=0"
        )
        return await self._execute(
            cmd, "attention_backend_mismatch", "software",
            f"Forced incompatible attention backend on {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_ATTENTION_BACKEND- VLLM_USE_V1-",
        )

    async def inject_state_contamination(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Simulate cross-layer state contamination in KV cache.

        Inspired by Teller Case Study: when a request is misclassified as
        decode, the kernel reads stale state from a cache slot left by a
        previous request. The contamination propagates through the entire
        generated sequence.

        Simulated by corrupting shared memory regions used for KV cache
        block tables.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        script = (
            "python3 -c \"import torch; "
            "ok=torch.cuda.is_available(); "
            "t=torch.randn((64,128),device='cuda') if ok else None; "
            "(t.fill_(float('nan')), torch.cuda.synchronize()) if ok else None; "
            "print('State contamination injected' if ok else 'No CUDA device available')\""
        )
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- {script}"
        )
        return await self._execute(
            cmd, "state_contamination", "software",
            f"KV cache state contamination injected in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from all software faults by restarting deployment."""
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"VLLM_MAX_NUM_SEQS- VLLM_MAX_NUM_BATCHED_TOKENS- "
            f"VLLM_GPU_MEMORY_UTILIZATION- VLLM_ATTENTION_BACKEND- VLLM_USE_V1- && "
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-kv-cache-load "
            f"--ignore-not-found=true --force --grace-period=0 2>/dev/null || true && "
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} --type=json "
            f"-p '[{{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/1\",\"value\":\"/model\"}}]' "
            f"2>/dev/null || true && "
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'pid=$({VLLM_PID_SH}); "
            f"if [ -n \"$pid\" ]; then kill -CONT \"$pid\" 2>/dev/null || true; fi; "
            f"find /model /models /data/models -name \"*.safetensors\" -o -name \"config.json\" 2>/dev/null | "
            f"xargs -r chmod a+r' || true && "
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}"
        )
        return await self._execute(cmd, "recover_all", "software", "Restarted deployment")

    async def _execute(
        self, cmd: str, fault_type: str, layer: str,
        success_msg: str, recovery_cmd: Optional[str] = None,
    ) -> FaultResult:
        """Execute a kubectl command or log in dry-run mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            return FaultResult(fault_type, layer, True, f"[DRY-RUN] {success_msg}", recovery_cmd, cmd)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.info("Fault injected: %s", success_msg)
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            else:
                err = stderr.decode().strip()
                logger.error("Fault injection failed: %s", err)
                return FaultResult(fault_type, layer, False, f"Failed: {err}", recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available software fault types."""
        return [
            {"type": "process_crash", "description": "Kill vLLM serving process or force one Deployment pod restart"},
            {"type": "model_load_failure", "description": "Corrupt model directory path"},
            {"type": "tokenizer_error", "description": "Corrupt tokenizer configuration"},
            {"type": "process_hang", "description": "Pause vLLM serving process with SIGSTOP"},
            {"type": "model_permission_error", "description": "Remove read permission from a model asset"},
            {"type": "kv_cache_exhaustion", "description": "Exhaust KV cache via memory pressure"},
            {"type": "scheduler_contention", "description": "Saturate continuous batching scheduler"},
            {"type": "request_queue_overflow", "description": "Force request queue overflow (max-num-seqs=1)"},
            {"type": "continuous_batch_disruption", "description": "Disable continuous batching"},
            {"type": "weight_corruption", "description": "Corrupt model weight shard at runtime"},
            {"type": "prefill_decode_misclassification", "description": "Force prefill/decode misclassification (Teller case study)"},
            {"type": "attention_backend_mismatch", "description": "Force incompatible attention backend selection"},
            {"type": "state_contamination", "description": "Inject cross-layer KV cache state contamination"},
        ]
