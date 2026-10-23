# Skill Runtime Real Fault Tests

Date: 2026-06-28

Environment:

- Control node: `snail`
- Skill Runtime: `http://127.0.0.1:8090`, health returned `{"status":"ok","skills":9}`
- Kubernetes context: `kubernetes-admin@kubernetes`
- Test namespace: `skill-realtest` created for the experiment and deleted after tests
- Test image: `nginx:1.27-alpine`

## Result Summary

| Scenario | Injection | Skill diagnosis | Healing action | Result |
| --- | --- | --- | --- | --- |
| ServiceUnavailable | `kubectl scale deployment/sre-svcapp --replicas=0` | `k8s.service_unavailable` matched, confidence `0.88` | `kubectl scale deployment/sre-svcapp -n skill-realtest --replicas=1` | Closed loop; deployment returned to `1/1`, endpoints restored |
| PodCrashLoop | Patched container command to `exit 1` | `k8s.pod_crashloop` matched, confidence `0.87` | `kubectl rollout undo deployment/sre-crashapp -n skill-realtest` | Closed loop; deployment rolled back and available |
| ImagePullError | `kubectl set image deployment/sre-imageapp nginx=nginx:not-a-real-agenticsre-tag-260628b` | `k8s.image_pull_error` matched, confidence `0.88` | `kubectl set image deployment/sre-imageapp nginx=nginx:1.27-alpine -n skill-realtest` | Closed loop after fix; deployment returned to `1/1` |
| LLM/vLLM Process Crash, K8s mode | `kubectl scale deployment/vllm-sim --replicas=0` in `llm-skill-realtest` | `llm.vllm_process_crash` matched, confidence `0.78` in dry-run/fallback mode | `kubectl scale deployment/vllm-sim -n llm-skill-realtest --replicas=1` | Closed loop; deployment returned to `1/1`, endpoints restored |
| Bare-metal GPU/vLLM readiness | Read-only SSH inspection through jump host `222.200.180.22` | `tool.llm_host_readiness` classified `33.33.33.127` as `vllm_process_missing` and `33.33.33.128` as `ready` | No mutation; generated read-only GPU/process/health/metrics checks | 127 has three T4 GPUs but no vLLM; 128 has three T4 GPUs, running vLLM container, model API, and `vllm:` metrics |
| K8S vLLM Pod Delete, real T4 cluster | `kubectl -n llm-k8s delete pod -l app=vllm-server --wait=false` on the 127/128 kubeadm cluster | `llm.k8s_vllm_observability` covers endpoint failure, Deployment readiness, Prometheus target, request counters, TTFT P95, and E2E P95 | Deployment recreated the vLLM Pod; skill recovery plan includes scale/rollout restart with rollback and inference verification | Closed loop; new Pod was created within 2s, vLLM served HTTP 200 again at ~18s, Prometheus target stayed `up`, 30/30 background requests succeeded before injection |
| Qwen GPU vLLM scale-to-zero, real T4 cluster | `kubectl -n llm-k8s scale deployment/vllm-server --replicas=0` | `llm.k8s_vllm_observability` primary, 12 matched skills, confidence `0.908`; `llm.vllm_process_crash` also matched with confidence `0.914` | `kubectl -n llm-k8s scale deployment/vllm-server --replicas=1` followed by rollout wait and business inference probe | Closed loop; Endpoint changed from `<none>` back to `10.244.1.81:8000`, Qwen returned a Chinese completion after recovery |
| Qwen vLLM Service selector miswire, real T4 cluster | Patched `service/vllm-server` selector to `app=vllm-server-miswired` | `llm.k8s_vllm_observability` classified `LLM::k8s_vllm_service_endpoints_empty`, confidence `0.908` | Restored selector to `app=vllm-server` | Closed loop; `endpoints/vllm-server` changed from `<none>` back to `10.244.1.81:8000`; vLLM Pod stayed running |
| Qwen business SLO latency regression, real T4 cluster | 25s in-cluster load, concurrency `8`, prompt len `256`, max tokens `32` | `llm.k8s_vllm_observability` classified `LLM::k8s_vllm_high_latency`, confidence `0.922` | No mutating heal; load ended and SLO returned to observation-only state | Fault propagation observed; `185/185` requests succeeded, latency P95 `1.1335s`, Prometheus E2E P95 about `1.47s` versus previous baseline about `0.49s` |
| Prometheus metrics path down, real T4 cluster | `kubectl -n llm-k8s scale deployment/vllm-prometheus --replicas=0` | `llm.k8s_vllm_observability` classified `LLM::k8s_vllm_metrics_target_down`, confidence `0.852` | Restored `deployment/vllm-prometheus --replicas=1` | Closed loop; `endpoints/vllm-prometheus` restored to `10.244.1.84:9090`; vLLM business endpoint was not interrupted |
| Business bad-model request failures, real T4 cluster | `curl` to `/v1/completions` with `model=not-a-real-model` returned HTTP `404`; replay payload represented a 20-request failed window while endpoint status stayed `ok` | `llm.k8s_vllm_observability` classified `LLM::k8s_vllm_request_failures`, confidence `0.922` | No service restart recommended; inspect model name, request parameters, and client configuration | Diagnostic branch fixed; request failures no longer collapse into generic observed state or service restart advice |

## Finding And Fix

The first ImagePullError test showed accurate diagnosis but incomplete healing:
the skill only suggested describe/secret checks and rollout restart. That cannot
fix a bad image tag.

Fix applied:

- `k8s.image_pull_error` now accepts `container`, `current_image`, and
  `last_good_image` or `expected_image`.
- When a verified image is provided, the skill emits a controlled
  `kubectl set image deployment/<deployment> <container>=<last_good_image>`
  action with rollback to the bad/current image.
- `configs/heal_recipes.yaml` now includes the same image recovery template.
- The legacy fallback adapter can materialize `<container>`, `<current_image>`,
  and `<last_good_image>` placeholders.

The LLM/vLLM K8s-mode test exposed a second issue: the standalone skill runtime
container initially depended on legacy LLM confidence calibration, which made
`diagnose` fragile and could prevent `heal` suggestions from being generated.

Fix applied:

- `llm.vllm_process_crash` now uses `skill_runtime.confidence` directly and
  returns the migrated `llm_fault_observable_signals_v1` explanation without
  requiring the legacy adapter.
- Generic workflow diagnoses use `skill_evidence_calibrated_v2`, including
  explicit provenance checks, diagnosis-history-only calibration, and caps for
  weak evidence and bridge skills. Recovery outcomes are reserved for the
  automatic execution gate.
- The skill now emits both a scale-from-zero recovery action and a rollout
  restart action for K8s deployments.
- Endpoint checks are parameterized through `endpoint` or `vllm_endpoint`.

The bare-metal GPU pass validated that the skill path can consume real host
observations without hardcoded credentials or service mutations.

Observation:

- `33.33.33.127`: SSH reachable; `nvidia-smi` reports three Tesla T4 GPUs;
  no Kubernetes CLI; no active vLLM service on `127.0.0.1:8000`.
- `33.33.33.128`: SSH reachable; `nvidia-smi` reports three Tesla T4 GPUs;
  Docker container `agenticsre-vllm-smoke` is running; `/v1/models` exposes
  `qwen2.5-coder-3b`; `/metrics` returns `vllm:` samples including request
  counters, cache usage, and throughput gauges.

Fix applied:

- Added `tool.llm_host_readiness` as a read-only host skill for GPU visibility,
  vLLM process presence, health endpoint, and metrics endpoint checks.
- `llm.vllm_process_crash` host recovery remains high risk and approval-gated.
- Policy now treats restart permissions as mutating permissions, not only
  `.write` and `.admin` permissions.

The real K8S vLLM pass validated the intelligent-computing closed loop on the
T4 kubeadm cluster:

- `33.33.33.127` was initialized as the control-plane node and `33.33.33.128`
  joined as the GPU worker node.
- NVIDIA device plugin registered `nvidia.com/gpu=3` on `dds-128`.
- Existing Docker vLLM on GPU0 was preserved; K8S vLLM was pinned at runtime to
  a free GPU via `NVIDIA_VISIBLE_DEVICES` and loaded
  `qwen2.5-coder-3b-k8s`.
- K8S vLLM health, model API, and `/metrics` were reachable through NodePort
  `30080`.
- K8S Prometheus was deployed through NodePort `30090` and scraped
  `vllm-server.llm-k8s.svc.cluster.local:8000/metrics` every 5s.
- Background load generated 30 successful inference requests; Prometheus
  reported `request_success_total`, E2E P95 around `0.785s`, and TTFT P95
  around `0.059s` in the 2-minute query window.
- During Pod deletion, requests failed until the new vLLM process finished
  loading the model. HTTP 200 resumed at about 18 seconds, confirming
  Kubernetes self-healing plus business-side observability.

Fix applied:

- Added `llm.k8s_vllm_observability` for K8S-hosted vLLM fault detection,
  diagnosis, guarded recovery, and verification.
- Added reusable `deploy/k8s-vllm/` manifests and scripts. Runtime-specific
  values such as node name, GPU UUID, model path, and served model name are
  provided through environment variables instead of hardcoded into the
  deployment script.

Regression:

- `python3 -m pytest tests/e2e/test_skill_runtime.py -q`
- Result: `20 passed`
- `python3 -m pytest tests/e2e -q`
- Result: `64 passed`

## Safety Notes

- All writes were limited to the isolated `skill-realtest` namespace.
- Skill Runtime still marks write actions as approval-required and does not set
  `safe_auto_execute=true`.
- The real remediation commands were executed manually from the generated
  suggestions to validate that the suggestions are actionable.
- The test namespace was deleted after the experiment.
- Bare-metal GPU readiness validation was read-only: no network fault injection,
  process kill, service restart, or GPU stress was executed.

## 2026-07-01 Gemma 3 Readiness Check

The T4 kubeadm cluster was checked as the target for Gemma-based intelligent
computing fault tests.

Observed facts:

- Local HF-format Gemma 3 weights were found on the shared data volume:
  `/data/dds-data/cxw/models/Gemma-3/1B/LLM-Research/gemma-3-1b-it` and
  `/data/dds-data/cxw/models/Gemma-3/4B/LLM-Research/gemma-3-4b-it`.
- The cached Gemma 4 12B path
  `/data/dds-data/hf-cache/hub/models--google--gemma-4-12B-it` contains only
  `config.json`; local weight bytes were `0`, so it cannot be deployed.
- The cached `vllm/vllm-openai:latest` image is vLLM `0.6.5` with
  Transformers `4.47.1`; native vLLM loading fails with
  `KeyError: 'gemma3_text'`.
- Cached Python dependencies on `/data/dds-data/uv_cache` include
  Transformers `4.57.1`, tokenizers, huggingface_hub, and safetensors.
- Direct outbound access from `dds-127` to HuggingFace and Docker Hub failed
  with connection resets, so the node cannot directly pull Gemma 3 weights or a
  newer vLLM image.
- The existing `vllm-gemma` deployment had been configured for
  `tensor_parallel_size=3`. That is invalid for the available Gemma-2-9B model
  because it has 16 attention heads and 16 is not divisible by 3.
- `dds-127` still showed CUDA initialization failures in a fresh torch GPU Pod;
  `dds-128` successfully ran torch CUDA and was used for Gemma 3 validation.

Fix applied:

- Gemma 3 1B was used only as a smoke test for the HF OpenAI-compatible runtime;
  it must not be used as the main intelligent-computing fault target.
- The AI cluster profile now defaults the T4 closure target to
  `qwen2.5-coder-3b-k8s` via `LLM_K8S_T4_MODEL`, and records that Gemma 4
  weights are absent while Gemma 3 4B is not yet reliable on the T4 GPU fp16
  path.
- LLM SLO and K8S vLLM observability skills no longer default their validation
  commands to the old `qwen2.5-coder-3b-k8s` model. They inherit the model from
  the workflow payload or use a generic `VLLM_MODEL` placeholder.

Validation result:

- A real Gemma 3 1B smoke run returned HTTP 200 and 52/52 successful in-cluster
  background-load requests, but this model is too small for the target fault
  experiment.
- A Gemma 3 4B GPU Pod recognized `Gemma3Processor` and loaded roughly 8.3 GiB
  onto one T4. After mounting cached Torch `2.8.0+cu128`, Transformers `4.57.1`,
  torchvision `0.23.0`, and CUDA 12.8 wheel dependencies, the Gemma 3 processor
  path ran without the earlier torch `2.6` mask API error. The GPU fp16 forward
  pass still produced `NaN` logits and generation repeatedly returned only
  `<pad>` tokens. The same prompt on CPU float32 produced finite logits, so the
  current T4 path is a numeric precision/runtime issue rather than missing
  weights or a broken chat template. Do not claim Gemma 3 4B is closed on T4
  until a quantized path, CPU fallback, or bf16-capable GPU runtime is validated.
- A Qwen2.5 14B attempt confirmed complete 29.5GB local weights, but FP16 vLLM
  loading from NFS on 2 T4s stalled at checkpoint loading and was stopped to
  release GPUs.

Remaining items before declaring full Gemma 3 fault-injection closure:

- Load a newer vLLM image that supports Gemma 3 onto both GPU nodes, for
  example by saving it on a network-enabled host and importing it with
  `ctr -n k8s.io images import`, if native vLLM metrics are required.
- Fix the deploy-node to `33.33.33.128:30081` NodePort path or use a jump-host /
  node-local tunnel. The service is reachable from `dds-128`, but direct
  requests from the deployment node timed out during this test.
- Complete Gemma 3 4B serving on NodePort `30081`, then re-run bounded
  background load and at least one inject-diagnose-heal scenario against
  `deployment=vllm-gemma`, `service=vllm-gemma`, and `model=gemma-3-4b-it`.

## 2026-07-01 Qwen GPU Closed-Loop Fallback

Because Gemma 3 4B is not numerically stable on the T4 fp16 path, the real
end-to-end intelligent-computing closure was re-run with the validated Qwen GPU
service.

Observed facts:

- `deployment/vllm-gemma` and `deployment/vllm-large` were scaled to `0`; all
  three T4 GPUs on `dds-128` were idle before restoring Qwen.
- `deployment/vllm-server` was scaled to `1` on `dds-128` with served model
  `qwen2.5-coder-3b-k8s`; vLLM loaded 5.79 GiB of model weights on GPU 1.
- `/v1/models` exposed `qwen2.5-coder-3b-k8s`, and a Chinese completion request
  returned HTTP 200.
- A 30s in-cluster load run against
  `http://vllm-server.llm-k8s.svc.cluster.local:8000/v1/completions` completed
  `69/69` requests successfully, `2.3 rps`, with latency P95 about `0.445s`.
- Prometheus scraped `vllm:num_requests_running`,
  `vllm:num_requests_waiting`, `vllm:request_success_total`,
  `vllm:time_to_first_token_seconds_bucket`, and
  `vllm:e2e_request_latency_seconds_bucket` with model label
  `qwen2.5-coder-3b-k8s`.

Fault closure:

- Injection: scaled `deployment/vllm-server` to `0`.
- Effect: `deployment/vllm-server` became `0/0`, `endpoints/vllm-server`
  became `<none>`, and the service had no backing Pod.
- Diagnosis: skill workflow matched 12 skills. Primary was
  `llm.k8s_vllm_observability`; diagnosis was
  `LLM::k8s_vllm_unavailable`, root cause
  `K8S vLLM Pod 不可用或模型服务尚未完成重建`, component `vllm-server`,
  confidence `0.908`. Evidence included endpoint failure, metrics availability,
  and `Deployment Ready/Desired=0/1`.
- Self-healing: scaled `deployment/vllm-server` back to `1` and waited for
  rollout.
- Verification: Endpoint restored to `10.244.1.81:8000`; a Chinese completion
  request returned HTTP 200 with model `qwen2.5-coder-3b-k8s`.

This is the current validated GPU path for demonstrations and regression tests.
Gemma remains documented as a target, but not as the default T4 closure model.

Additional Qwen fault validation on 2026-07-01:

- Service discovery fault: patching `service/vllm-server` to a mismatched
  selector made `endpoints/vllm-server` empty while
  `deployment/vllm-server` remained `1/1`. The skill now distinguishes this
  from Pod/process failure as `LLM::k8s_vllm_service_endpoints_empty`.
- Business-side load propagation: a bounded in-cluster load with concurrency
  `8` produced `185/185` successful requests, load-generator P95 `1.1335s`,
  Prometheus TTFT P95 about `0.078s`, and Prometheus E2E P95 about `1.47s`.
  The skill now classifies the condition as `LLM::k8s_vllm_high_latency`
  rather than treating missing Deployment readiness fields as `0/1`.
- Metrics-plane fault: scaling Prometheus to `0` made NodePort `30090` and
  `endpoints/vllm-prometheus` unavailable while Qwen inference stayed healthy.
  The skill classified this as `LLM::k8s_vllm_metrics_target_down`.
- Business request failure: a real request using `model=not-a-real-model`
  returned HTTP `404` with `NotFoundError`. Replayed failed-request windows with
  endpoint status `ok` now map to `LLM::k8s_vllm_request_failures`, pointing
  operators at model name/request-parameter/client configuration checks instead
  of restarting the vLLM service.

## 2026-08-06 Remote Dual-Path Diagnosis Validation

This validation used the remote `snail/lsy-0` control node and its four-node
Kubernetes cluster. It did not use a local Kubernetes environment. All fault
resources were isolated in `agenticsre-diag-e2e-0806`; the namespace carried
both `app.kubernetes.io/managed-by=agenticsre-remote-live-diagnosis-e2e` and a
per-run owner label, and deletion was refused unless both labels matched.

The reusable driver is
`tests/e2e/test_live_diagnosis_fault_injection.py`. It rejects truth-bearing RCA
inputs such as `fault_type`, `root_cause`, prefilled confidence/evidence,
candidate faults, observation signals, and scenario IDs. Both modes therefore
received only the observed symptom and Kubernetes resource coordinates.

| Scenario | Multi-agent result | Agent+Skill result | Recovery |
|---|---|---|---|
| Deployment scaled from 1 to 0 | Run `rca-7cd3953c`; identified desired replicas 0, no Pod, and empty Service endpoints; calibrated confidence `0.75` after review cap | Run `rca-ac83439f`; six read-only checks; `k8s.service_unavailable`; confidence `0.806` | Scaled back to 1, rollout `1/1`, endpoints and business probe recovered |
| Nonexistent image tag | Run `rca-227726a3`; identified the exact missing tag and live `ImagePullBackOff`; calibrated confidence `0.75` after review cap | Run `rca-b319b887`; `k8s.image_pull_error`; confidence `0.809`; LLM reasoning cited the live Pod and image-pull evidence | Set image to `nginx:1.27-alpine`; rollout reached `1/1` |

The long-running SSH wrapper disconnected after the multi-agent image run had
already completed; the Dashboard background job remained intact. I resumed the
remaining Agent+Skill image run (`rca-b319b887`) with the same truth-leak-free
payload, then performed recovery and owner-checked cleanup separately. This is
why the four run IDs are recorded individually instead of treating one shell
session as the evidence boundary.

The remote pass exposed several failures that synthetic/pre-filled tests did
not catch:

- A Warning event for a deleted Pod could override the current Deployment and
  Endpoint state. EventAgent now separates orphaned events from active events
  and emits normalized live-state evidence.
- Cancelling an `asyncio.to_thread` LLM request did not bound the complete API
  duration because `asyncio.run()` waited for the default executor. Timed LLM
  work now uses cancellation-safe daemon workers.
- A generated final report could replace confirmed `replicas=0` evidence with
  an unsupported selector-mismatch explanation. Decisive live state now aligns
  contradictory report prose back to the confirmed Top1 mechanism.
- Agent+Skill reasoning could lose command output to prompt truncation and
  return `evidence_insufficient` despite successful checks and a successful
  Skill diagnosis. Compact command samples are now placed first, preserved as
  structured evidence, and protected by an evidence-consistency guard.
- The LLM could omit a Skill call because the read-only evidence was already
  sufficient, or select a generic Deployment skill for an explicit
  `ImagePullBackOff`. Agent+Skill now performs one independent matching Skill
  validation and prefers the most specific Skill for explicit runtime states.

After recovery and owner-checked cleanup, the test namespace was absent,
`lsy-1` through `lsy-4` were all Ready, and `kube-system` had no non-Running
Pods. The existing Dashboard and Skill Runtime on ports 8080/8090 were not
stopped or modified; validation used isolated ports 18080/18090.

## Final Worktree Remote Revalidation (2026-08-06)

The final worktree was synchronized to the isolated remote copy and the full
two-fault, two-path driver was rerun from a clean namespace. The suite ID was
`live-diag-bfa12c41f5`, and the complete machine-readable result is retained at
`docs/validation/remote-live-diagnosis-20260806.json`.

| Scenario | Multi-agent | Agent+Skill | Recovery |
|---|---|---|---|
| Deployment replicas set to 0 | `rca-b06e6559`; `service_disruption`; confidence `0.75`; `323.52s` | `rca-4de72f55`; `k8s.service_unavailable`; `ServiceUnavailable`; confidence `0.814`; `140.37s` | Deployment returned to `1/1`; endpoints and business probe recovered |
| Missing image tag | `rca-6f8922d6`; live `pod_waiting_state` overrode the recalled replicas hypothesis; `image_pull_error`; confidence `0.75`; `311.92s` | `rca-7552a55e`; `k8s.image_pull_error`; `ImagePullError`; confidence `0.723`; `115.364s` | Image changed to `nginx:1.27-alpine`; rollout reached `1/1` |

This rerun caught one additional real regression before acceptance: the
scale-to-zero Agent+Skill root-cause text was correct, but a recalled image-pull
hypothesis caused the fault type and Skill to be reported as `ImagePullError`.
Specific Skill overrides now use only current successful read-only command
output; recent-run context is isolated by both cluster and namespace; and the
final fault type is aligned with the evidence-supported reasoned root cause.
The RCA poll timeout is configurable through
`AGENTICSRE_LIVE_DIAG_RCA_TIMEOUT` and defaults to 720 seconds because a valid
quality-closure run took 411.94 seconds.

The driver recorded `recovery_verified=true` for both scenarios and
`cleanup_verified=true`. The owner-checked namespace was absent afterward,
temporary ports 18080/18090 were stopped, all four nodes were Ready,
`kube-system` had no unhealthy Pod, and production ports 8080/8090 remained
healthy and unchanged.
