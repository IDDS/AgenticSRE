# AISHPerf / Alcor AIOps Integration

## Source

- Repository: https://gitee.com/aishperf-caict/aishperf_openness
- Fault injector: `entities/aiops-chaos`
- Diagnosis benchmark: `suites/aiops-eval`
- Article reference: https://www.qbitai.com/2026/06/440454.html

## What Can Be Integrated

AISHPerf `aiops-chaos` provides an AI-infrastructure chaos framework with two
components:

- `chaos`: atomic fault CLI with `inject`, `recover`, and `validate` phases.
- `aemu`: Kubernetes-native orchestration service that schedules executor Pods
  and exposes HTTP APIs for fault injection and recovery.

Its open `new_config.yaml` currently contains 107 cases. The useful coverage for
AgenticSRE is:

| Domain | Examples | AgenticSRE Target |
| --- | --- | --- |
| GPU state/driver | `gpu-lost`, `gpu-xid31`, `ecc-xid48`, `high-temp`, `old-driver` | GPU Lab fault catalog, DCGM/TELLER diagnosis |
| RDMA/distributed training | `rdma-link-down`, `nccl-acs`, `metax-rdma-gid-index-inconsistent` | AI cluster topology edge fault, NCCL/RDMA diagnosis skill |
| Storage | `gpfs-lost`, `gpfs-full`, `nfs-unmount`, `nvme-unmount` | AI cluster storage dependency fault |
| Network | `iface-mtu`, `dns-err`, link-down cases | topology edge injection |
| Kubernetes/workload | `empty-endpoints`, image/runtime/workload cases | common fault lab and alert center |
| Security/process | `mining`, `pty-drain`, `sshd-lost`, `zombie` | review-only; never auto-execute |
| Heterogeneous accelerators | Ascend, Biren, Metax, MThreads, Tianshu cases | future vendor-specific AI cluster profiles |

## Current Integration Mode

The first AgenticSRE integration is intentionally conservative:

1. Read AISHPerf cases from `AISHPERF_OPENNESS_DIR`.
2. Expose a read-only scenario catalog through Web APIs.
3. Generate AEMU dry-run plans for review.
4. Add a Skill Runtime diagnosis adapter for AISHPerf AI Infra incidents.
5. Reuse `aiops-eval` metrics as evaluation targets.

The integration does not execute AISHPerf commands automatically. Many cases use
host namespaces, GPU/RDMA/PCI/storage mutations, or security simulations, so real
execution must pass AgenticSRE risk gates, approval, rollback, and verification.

## APIs

- `GET /api/integrations/aishperf/status`
- `GET /api/integrations/aishperf/scenarios?domain=gpu`
- `GET /api/integrations/aishperf/scenarios/{case_id}`
- `POST /api/integrations/aishperf/scenarios/{case_id}/dry-run`

The existing `GET /api/llm-faultlab/scenarios` also returns
`external_sources.aishperf` so the GPU Lab frontend can render an external
scenario tab.

## Skill Runtime

`llm.aishperf_aiinfra_diagnosis` maps AISHPerf/AEMU context into AgenticSRE's
detect, diagnose, heal, and verify stages. It covers:

- GPU hardware/driver/state anomalies.
- RDMA/NCCL/HCCL communication faults.
- GPFS/NFS/disk/storage faults.
- Kubernetes workload/service faults.
- Container runtime and distributed-training incidents.

It emits Chinese diagnosis summaries and recovery suggestions, but marks real
AEMU recovery as high risk.

## Diagnosis Method Integration

`suites/aiops-eval` contributes a diagnosis benchmark loop:

1. Use dataset metadata `aemu_fault_id`.
2. Generate fault through AEMU.
3. Ask the Agent to diagnose.
4. Recover fault through AEMU.
5. Score root-cause match and operational metrics.

AgenticSRE should reuse these metrics:

- `diagnosis_pass_rate`
- `fault_injection_success_rate`
- `recovery_success_rate`
- `evaluation_coverage`
- `avg_turns`
- `avg_latency_s`
- `error_rate`

These metrics should be added to future Skill/Agent regression tests for AI
cluster diagnosis.

## Deployment Notes

Set the source directory before starting the web app:

```bash
export AISHPERF_OPENNESS_DIR=/opt/aishperf_openness
```

For real execution, deploy AISHPerf `aemu` in the target K8S cluster and provide:

```bash
export AEMU_URL=http://<aemu-service>:8088
```

Do not enable automatic execution until:

- Target nodes are explicitly allowlisted.
- Dangerous domains are disabled or approval-gated.
- Cleanup/recover has been forward-tested.
- AgenticSRE can verify recovery through K8S, Prometheus, DCGM, vLLM, and TELLER
  signals.
