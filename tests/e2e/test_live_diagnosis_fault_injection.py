#!/usr/bin/env python3
"""Remote live fault-injection validation for RCA and Agent+Skill.

The test creates and deletes a dedicated Kubernetes namespace. It is disabled
unless ``AGENTICSRE_RUN_LIVE_DIAGNOSIS=1`` is set. Unlike the legacy live
self-healing script, diagnosis truth is never included in the API request.

Expected execution location: a remote AgenticSRE deployment host with kubectl
access to a non-production test cluster.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib import error, request


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
NAMESPACE = os.environ.get("AGENTICSRE_LIVE_DIAG_NS", "agenticsre-diag-e2e-0806")
APP = os.environ.get("AGENTICSRE_LIVE_DIAG_APP", "agenticsre-diag-probe")
CLIENT = f"{APP}-client"
IMAGE_APP = f"{APP}-image"
IMAGE = os.environ.get("AGENTICSRE_LIVE_DIAG_IMAGE", "nginx:1.27-alpine")
MISSING_IMAGE = os.environ.get(
    "AGENTICSRE_LIVE_DIAG_MISSING_IMAGE",
    "nginx:agenticsre-missing-live-diagnosis-tag",
)
RUN_ID = f"live-diag-{uuid.uuid4().hex[:10]}"
MANAGED_BY = "agenticsre-remote-live-diagnosis-e2e"
FORBIDDEN_DIAGNOSIS_KEYS = {
    "candidate_faults",
    "candidate_skills",
    "confidence",
    "diagnosis",
    "evidence",
    "experiment_fault_type",
    "fault_type",
    "initial_evidence",
    "observation_signals",
    "raw_fault_type",
    "root_cause",
    "scenario_id",
}


def kubectl(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["kubectl", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed ({proc.returncode})\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def api(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {raw}") from exc


def wait_for(
    condition: Callable[[], bool],
    label: str,
    *,
    timeout: int = 120,
    interval: float = 2.0,
) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            if condition():
                return
        except Exception as exc:  # keep the last transient observation
            last_error = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"timeout waiting for {label}: {last_error}")


def assert_safe_identifiers() -> None:
    if not NAMESPACE.startswith("agenticsre-diag-e2e-"):
        raise RuntimeError(f"unsafe namespace: {NAMESPACE!r}")
    if not APP.startswith("agenticsre-diag-"):
        raise RuntimeError(f"unsafe app name: {APP!r}")


def assert_no_truth_leak(value: Any, path: str = "request") -> None:
    if isinstance(value, dict):
        leaked = sorted(FORBIDDEN_DIAGNOSIS_KEYS.intersection(value))
        if leaked:
            raise AssertionError(f"diagnosis truth leaked at {path}: {leaked}")
        for key, item in value.items():
            assert_no_truth_leak(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_truth_leak(item, f"{path}[{index}]")


def namespace_exists() -> bool:
    return kubectl("get", "namespace", NAMESPACE, check=False).returncode == 0


def namespace_labels() -> Dict[str, str]:
    obj = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json").stdout)
    return ((obj.get("metadata") or {}).get("labels") or {})


def setup_namespace() -> None:
    if namespace_exists():
        raise RuntimeError(f"refusing to reuse existing namespace {NAMESPACE}")
    kubectl("create", "namespace", NAMESPACE)
    kubectl(
        "label",
        "namespace",
        NAMESPACE,
        f"app.kubernetes.io/managed-by={MANAGED_BY}",
        f"agenticsre.io/run-id={RUN_ID}",
    )


def setup_workload() -> None:
    kubectl("-n", NAMESPACE, "create", "deployment", APP, f"--image={IMAGE}")
    kubectl("-n", NAMESPACE, "expose", "deployment", APP, "--port=80", "--target-port=80")
    kubectl(
        "-n",
        NAMESPACE,
        "run",
        CLIENT,
        f"--image={IMAGE}",
        "--restart=Never",
        "--command",
        "--",
        "sleep",
        "3600",
    )
    kubectl("-n", NAMESPACE, "rollout", "status", f"deployment/{APP}", "--timeout=120s", timeout=130)
    wait_for(client_ready, "probe client Ready", timeout=120)
    wait_for(service_has_endpoints, "baseline service endpoints", timeout=60)
    baseline = business_probe()
    if baseline.returncode != 0 or "Welcome to nginx" not in baseline.stdout:
        raise AssertionError(f"baseline business probe failed: {baseline.stdout} {baseline.stderr}")


def client_ready() -> bool:
    value = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "pod",
        CLIENT,
        "-o",
        "jsonpath={.status.containerStatuses[0].ready}",
        check=False,
    ).stdout.strip()
    return value == "true"


def service_has_endpoints() -> bool:
    obj = json.loads(kubectl("-n", NAMESPACE, "get", "endpoints", APP, "-o", "json").stdout)
    return any(subset.get("addresses") for subset in obj.get("subsets") or [])


def business_probe() -> subprocess.CompletedProcess[str]:
    return kubectl(
        "-n",
        NAMESPACE,
        "exec",
        CLIENT,
        "--",
        "wget",
        "-q",
        "-T",
        "3",
        "-O",
        "-",
        f"http://{APP}",
        timeout=15,
        check=False,
    )


def poll_rca(run_id: str, *, timeout: Optional[int] = None) -> Dict[str, Any]:
    if timeout is None:
        timeout = max(60, int(os.environ.get("AGENTICSRE_LIVE_DIAG_RCA_TIMEOUT", "720")))
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = api("GET", f"/api/rca/{run_id}?_={int(time.time() * 1000)}", timeout=30)
        if last.get("status") in {"completed", "failed"}:
            return last
        time.sleep(2)
    raise TimeoutError(f"RCA run {run_id} did not finish: {json.dumps(last, ensure_ascii=False)[:2000]}")


def run_rca(query: str, *, skill: bool, deployment: str, service: str = "", pod: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "query": query,
        "namespace": NAMESPACE,
        "dedup_key": f"{RUN_ID}-{'skill' if skill else 'multi'}-{deployment}",
    }
    if skill:
        payload = {
            "source": "remote_business_observation",
            "domain": "k8s",
            "cluster_id": "default",
            "cluster_kind": "general",
            "namespace": NAMESPACE,
            "deployment": deployment,
            "service": service,
            "pod": pod,
        }
        payload = {key: value for key, value in payload.items() if value}
        body["skill_workflow"] = {
            "domain": "k8s",
            "stages": ["detect", "diagnose"],
            "analysis_phases": ["context", "hypothesis", "plan", "investigation", "reasoning"],
            "dry_run": True,
            "payload": payload,
        }
    assert_no_truth_leak(body)
    started = api("POST", "/api/rca/run", body, timeout=30)
    run_id = str(started.get("run_id") or "")
    if not run_id:
        raise AssertionError(f"missing run_id: {started}")
    result = poll_rca(run_id)
    if result.get("status") != "completed":
        raise AssertionError(f"RCA failed: {json.dumps(result, ensure_ascii=False)[:4000]}")
    result["request_truth_leak_checked"] = True
    return result


def diagnosis_view(run: Dict[str, Any], *, skill: bool) -> Dict[str, Any]:
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    if not skill:
        rca_envelope = result.get("result") if isinstance(result.get("result"), dict) else {}
        diagnosis = rca_envelope.get("result") if isinstance(rca_envelope.get("result"), dict) else {}
        return {
            "run_id": run.get("id"),
            "session_id": rca_envelope.get("session_id") or "",
            "root_cause": diagnosis.get("root_cause") or "",
            "fault_type": diagnosis.get("fault_type") or "",
            "confidence": diagnosis.get("confidence"),
            "root_cause_alignment": diagnosis.get("root_cause_alignment"),
            "human_review": rca_envelope.get("human_review"),
            "status": run.get("status"),
        }
    runtime = result.get("skill_runtime_result") if isinstance(result.get("skill_runtime_result"), dict) else {}
    diagnosis = runtime.get("diagnosis") if isinstance(runtime.get("diagnosis"), dict) else {}
    skill_runtime = runtime.get("skill_runtime") if isinstance(runtime.get("skill_runtime"), dict) else {}
    return {
        "run_id": run.get("id"),
        "root_cause": result.get("root_cause") or diagnosis.get("root_cause") or "",
        "fault_type": diagnosis.get("fault_type") or "",
        "confidence": result.get("confidence", diagnosis.get("confidence")),
        "primary_skill_id": skill_runtime.get("primary_skill_id") or diagnosis.get("skill_id") or "",
        "status": run.get("status"),
        "llm_used": any(
            event.get("event") in {"llm_hypotheses", "llm_hypothesis_result", "llm_reasoning_result"}
            and event.get("success")
            for event in run.get("events") or []
            if isinstance(event, dict)
        ),
    }


def assert_contains_any(text: str, expected: Iterable[str], label: str) -> None:
    normalized = text.casefold()
    if not any(token.casefold() in normalized for token in expected):
        raise AssertionError(f"{label} did not identify expected mechanism: {text!r}")


def assert_scale_to_zero_diagnosis(view: Dict[str, Any], *, skill: bool) -> None:
    combined = f"{view.get('root_cause', '')} {view.get('fault_type', '')}"
    assert_contains_any(combined, ["replica", "副本", "endpoint", "端点", "后端", "ready", "就绪"], "scale-to-zero diagnosis")
    if any(token in combined.casefold() for token in ("imagepull", "镜像拉取", "oom", "crashloop")):
        raise AssertionError(f"scale-to-zero diagnosis selected an unrelated mechanism: {combined}")
    if any(token in combined.casefold() for token in ("selector", "标签选择器", "标签不匹配")):
        raise AssertionError(f"scale-to-zero diagnosis contradicted live replica evidence: {combined}")
    assert_contains_any(combined, ["replica", "副本", "desired replicas", "replicas=0"], "scale-to-zero root cause")
    if skill and view.get("primary_skill_id") != "k8s.service_unavailable":
        raise AssertionError(f"unexpected service diagnosis skill: {view}")


def assert_image_pull_diagnosis(view: Dict[str, Any], *, skill: bool) -> None:
    combined = f"{view.get('root_cause', '')} {view.get('fault_type', '')}"
    assert_contains_any(combined, ["imagepull", "pull image", "镜像", "拉取", "tag", "仓库"], "image-pull diagnosis")
    if skill and view.get("primary_skill_id") != "k8s.image_pull_error":
        raise AssertionError(f"unexpected image-pull diagnosis skill: {view}")


def diagnose_selected(query: str, assertion: Callable[..., None], **targets: str) -> Dict[str, Any]:
    """Allow independent acceptance of each paradigm without masking failures."""
    selected = os.environ.get("AGENTICSRE_LIVE_DIAG_PARADIGM", "both")
    if selected not in {"both", "multi", "skill"}:
        raise ValueError(f"invalid diagnosis paradigm: {selected}")
    results: Dict[str, Any] = {}
    for name, skill in (("multi", False), ("skill", True)):
        if selected not in {"both", name}:
            continue
        run = run_rca(query, skill=skill, **targets)
        view = diagnosis_view(run, skill=skill)
        print(json.dumps({"paradigm": name, "diagnosis": view}, ensure_ascii=False), flush=True)
        assertion(view, skill=skill)
        if skill and not view.get("llm_used"):
            raise AssertionError("Agent+Skill acceptance requires a successful real LLM call")
        results["agent_skill" if skill else "multi_agent"] = view
    return results


def scale_to_zero_scenario() -> Dict[str, Any]:
    kubectl("-n", NAMESPACE, "scale", f"deployment/{APP}", "--replicas=0")
    wait_for(lambda: not service_has_endpoints(), "service endpoints removed", timeout=60)
    # EndpointSlice/kube-proxy/conntrack propagation can briefly allow one more
    # request after the legacy Endpoints object becomes empty. Require the
    # business failure itself, but allow a bounded convergence interval.
    wait_for(
        lambda: business_probe().returncode != 0,
        "business probe failure after scale-to-zero",
        timeout=45,
        interval=2,
    )

    query = (
        f"远端业务探针无法访问 Kubernetes Service {APP}，目标位于 namespace {NAMESPACE}。"
        "请仅使用当前集群实时状态、事件和只读工具定位根因。"
    )
    diagnoses = diagnose_selected(query, assert_scale_to_zero_diagnosis, deployment=APP, service=APP)

    kubectl("-n", NAMESPACE, "scale", f"deployment/{APP}", "--replicas=1")
    kubectl("-n", NAMESPACE, "rollout", "status", f"deployment/{APP}", "--timeout=120s", timeout=130)
    wait_for(service_has_endpoints, "service endpoints restored", timeout=60)
    recovered_probe = business_probe()
    if recovered_probe.returncode != 0:
        raise AssertionError(f"business probe did not recover: {recovered_probe.stderr}")
    return {
        "scenario": "service_scale_to_zero",
        "ground_truth": "deployment desired replicas changed from 1 to 0, removing service endpoints",
        "input_truth_leak": False,
        "fault_observed": {"endpoints_empty": True, "business_probe_failed": True},
        **diagnoses,
        "recovery_verified": True,
    }


def image_pull_waiting() -> bool:
    obj = json.loads(
        kubectl("-n", NAMESPACE, "get", "pods", "-l", f"app={IMAGE_APP}", "-o", "json").stdout
    )
    for pod in obj.get("items") or []:
        for status in ((pod.get("status") or {}).get("containerStatuses") or []):
            reason = ((((status.get("state") or {}).get("waiting") or {}).get("reason")) or "")
            if reason in {"ErrImagePull", "ImagePullBackOff"}:
                return True
    return False


def image_pull_scenario() -> Dict[str, Any]:
    kubectl("-n", NAMESPACE, "create", "deployment", IMAGE_APP, f"--image={MISSING_IMAGE}")
    wait_for(image_pull_waiting, "real ImagePullBackOff", timeout=180, interval=3)
    pod = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "pods",
        "-l",
        f"app={IMAGE_APP}",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ).stdout.strip()
    if not pod:
        raise AssertionError("image-pull pod was not created")

    query = (
        f"远端 Kubernetes 工作负载 {IMAGE_APP} 长时间未达到 Ready，位于 namespace {NAMESPACE}。"
        "请仅使用当前集群实时状态、事件和只读工具定位根因。"
    )
    diagnoses = diagnose_selected(query, assert_image_pull_diagnosis, deployment=IMAGE_APP, pod=pod)

    kubectl("-n", NAMESPACE, "set", "image", f"deployment/{IMAGE_APP}", f"nginx={IMAGE}")
    kubectl("-n", NAMESPACE, "rollout", "status", f"deployment/{IMAGE_APP}", "--timeout=180s", timeout=190)
    return {
        "scenario": "image_pull_error",
        "ground_truth": f"deployment referenced nonexistent image {MISSING_IMAGE}",
        "input_truth_leak": False,
        "fault_observed": {"pod": pod, "waiting_reason": "ImagePullBackOff"},
        **diagnoses,
        "recovery_verified": True,
    }


def cleanup_namespace() -> None:
    if not namespace_exists():
        return
    labels = namespace_labels()
    if labels.get("app.kubernetes.io/managed-by") != MANAGED_BY:
        raise RuntimeError(f"refusing to delete namespace without managed-by label: {labels}")
    if labels.get("agenticsre.io/run-id") != RUN_ID:
        raise RuntimeError(f"refusing to delete namespace owned by another run: {labels}")
    kubectl("delete", "namespace", NAMESPACE, "--wait=true", "--timeout=180s", timeout=190)


def main() -> int:
    if os.environ.get("AGENTICSRE_RUN_LIVE_DIAGNOSIS") != "1":
        print("SKIP live diagnosis fault injection; set AGENTICSRE_RUN_LIVE_DIAGNOSIS=1")
        return 0
    assert_safe_identifiers()
    health = api("GET", "/api/health", timeout=10)
    if not health.get("llm_configured"):
        raise RuntimeError("remote Dashboard has no configured LLM")

    results: List[Dict[str, Any]] = []
    try:
        setup_namespace()
        setup_workload()
        results.append(scale_to_zero_scenario())
        results.append(image_pull_scenario())
    finally:
        cleanup_namespace()

    if namespace_exists():
        raise AssertionError(f"test namespace still exists after cleanup: {NAMESPACE}")
    summary = {
        "status": "passed",
        "paradigm": os.environ.get("AGENTICSRE_LIVE_DIAG_PARADIGM", "both"),
        "run_id": RUN_ID,
        "remote_dashboard": BASE_URL,
        "namespace": NAMESPACE,
        "truth_leak_protection": sorted(FORBIDDEN_DIAGNOSIS_KEYS),
        "scenarios": results,
        "cleanup_verified": True,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"LIVE_DIAGNOSIS_FAILED: {exc}", file=sys.stderr)
        raise
