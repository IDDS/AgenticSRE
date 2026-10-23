# OpenTelemetry Demo Benchmark

This directory contains AgenticSRE deployment material for the upstream
OpenTelemetry Demo benchmark.

Upstream:
- https://github.com/open-telemetry/opentelemetry-demo
- Helm chart: `open-telemetry/opentelemetry-demo`

## Deploy

```bash
REMOTE_HOST=snail ./benchmarks/opentelemetry-demo/deploy_remote.sh deploy
```

Defaults:
- namespace: `otel-demo`
- Helm release: `otel-demo`
- chart version: `0.40.9`
- app version: `2.2.0`
- frontend NodePort: `30088`
- Prometheus NodePort: `30091`

## Check Status

```bash
REMOTE_HOST=snail ./benchmarks/opentelemetry-demo/deploy_remote.sh status
```

## Run AgenticSRE Loops

Run the full recordable suite with three validated fault types:

```bash
ssh snail 'cd /tmp/agenticsre-otel-demo && \
  AGENTICSRE_RUN_RECORDING_SUITE=1 \
  AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
  python3 run_recording_suite.py'
```

The suite runs:
- Network partition -> detects outage -> deletes temporary `NetworkChaos`.
- CPU stress -> detects resource pressure -> deletes temporary stress pod.
- ImagePullBackOff -> detects bad image rollout -> rolls back `deployment/frontend`.

For each scenario the suite validates that the output contains background
load, observability metrics, fault detection evidence, diagnosis readiness,
self-healing success, and recovery verification.

The scale-to-zero loop validates service monitoring, diagnosis-gated healing,
and endpoint recovery:

```bash
ssh snail 'cd /tmp/agenticsre-otel-demo && \
  AGENTICSRE_RUN_OTEL_LOOP=1 \
  AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
  python3 run_agenticsre_loop.py'
```

The CPU stress loop creates a temporary stress pod, records CPU and business
metrics, then lets AgenticSRE delete the stress pod through the self-healing
API:

```bash
ssh snail 'cd /tmp/agenticsre-otel-demo && \
  AGENTICSRE_RUN_CPU_DEMO=1 \
  AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
  python3 run_cpu_stress_demo.py'
```

The image-pull loop patches `deployment/frontend` to a non-existing image tag,
records the `ImagePullBackOff` pod, then lets AgenticSRE roll the deployment
back to the previous working image:

```bash
ssh snail 'cd /tmp/agenticsre-otel-demo && \
  AGENTICSRE_RUN_IMAGE_DEMO=1 \
  AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
  python3 run_image_pull_demo.py'
```

The recordable network loop uses ChaosMesh to partition `frontend-proxy` from
`frontend`, then lets AgenticSRE retrieve and execute the approved remediation:

```bash
ssh snail 'cd /tmp/agenticsre-otel-demo && \
  AGENTICSRE_RUN_NETWORK_DEMO=1 \
  AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
  python3 run_network_policy_demo.py'
```

The network loop records:
- OTel `load-generator` background traffic status.
- Frontend HTTP transition: healthy, degraded, recovered.
- `NetworkChaos` creation and removal.
- OTel-native frontend request rate, P99 latency, 5xx rate, and proxy 5xx status from the demo Prometheus source.
- Container CPU and memory from the AgenticSRE cluster Prometheus source.
- Lightweight AgenticSRE alert snapshots.
- Diagnosis-gated self-healing plan, executed command, and recovery check.

Recommended recording sequence:
- Network partition: strongest visible business outage, HTTP 200 -> 503 -> 200.
- Scale-to-zero: endpoint disappearance and Deployment replica recovery.
- CPU stress: resource curve increase and cleanup of a temporary stress pod.
- ImagePullBackOff: rollout to bad image and rollback to the previous working image.

For a screen recording, keep AgenticSRE's `指标监控` and `告警中心` pages open
with namespace `otel-demo`, then run the network loop in a terminal. The
container charts show system-level changes. When namespace `otel-demo` is
selected, the business charts switch to the OTel Demo Prometheus source and
show frontend request rate, P99 latency, and 5xx rate.

By default, the automated recording keeps diagnosis results, editable healing
plans, and healing execution results on screen for at least 12 seconds so
narration does not cross a page transition. These minimum holds can be adjusted
for a slower presentation:

```bash
export AGENTICSRE_DEMO_DIAGNOSIS_RESULT_HOLD_SECONDS=16
export AGENTICSRE_DEMO_HEAL_PLAN_HOLD_SECONDS=16
export AGENTICSRE_DEMO_HEAL_EXECUTED_HOLD_SECONDS=16
python3 benchmarks/opentelemetry-demo/record_otel_demo_video.py
```

## Stop

```bash
REMOTE_HOST=snail ./benchmarks/opentelemetry-demo/deploy_remote.sh stop
```

## Notes

The values file keeps the demo microservice topology, collector, Prometheus,
and Jaeger enabled. OpenSearch and Grafana are disabled to reduce memory
pressure and avoid Docker Hub sidecar image pulls in the remote environment.
The collector log pipeline exports to `debug` instead of OpenSearch.

Access after deployment:
- Webstore: `http://<node-ip>:30088/`
- Prometheus: `http://<node-ip>:30091/`
- Jaeger UI: `http://<node-ip>:30088/jaeger/ui`
- Load generator: `http://<node-ip>:30088/loadgen/`
- Feature flags: `http://<node-ip>:30088/feature/`
