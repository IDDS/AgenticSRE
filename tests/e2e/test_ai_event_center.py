#!/usr/bin/env python3
"""Regression tests for the standalone AI event center detection layer."""

from __future__ import annotations

import pathlib
import sys
import asyncio
import json

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_app import app as webapp  # noqa: E402


def _dashboard_payload() -> dict:
    return {
        "cluster": {
            "id": "ai-t4",
            "name": "T4 vLLM",
            "default_namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen",
        },
        "storage": {"available": True},
        "summary": {
            "waiting_requests": 3,
            "running_requests": 1,
            "ttft_p95": 1.4,
            "e2e_latency_p95": 4.2,
            "kv_cache": 22,
            "gpu_util": 45,
            "gpu_memory_used": 52,
            "success_rate": 1,
        },
    }


def test_ai_event_detection_uses_llm_metrics_and_chinese_titles() -> None:
    events = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())

    types = {event["event_type"] for event in events}
    assert "vllm_queue_backlog" in types
    assert "inference_latency_high" in types
    assert all(event["domain"] == "llm" for event in events)
    assert any("vLLM 等待队列升高" == event["title"] for event in events)
    assert all(event["evidence"] for event in events)


def test_fault_action_automatically_refreshes_metric_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")

    async def fake_light_payload(cluster, *, duration="5m", prometheus_url=""):
        return {
            "cluster": cluster,
            "storage": {"available": True},
            "summary": {"kv_cache": 95, "gpu_memory_used": 40, "success_rate": 1},
            "peaks": {"kv_cache": 95},
        }

    monkeypatch.setattr(webapp, "_ai_event_light_dashboard_payload", fake_light_payload)
    monkeypatch.setattr(webapp, "_generate_ai_events_from_fault_runs", lambda *args, **kwargs: [])
    cluster = {
        "id": "ai-t4",
        "kind": "ai",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
    }

    result = asyncio.run(webapp._refresh_ai_events_after_fault_action(cluster))

    assert result["triggered"] is True
    assert result["active_count"] >= 1
    assert any(event["event_type"] == "gpu_memory_pressure" for event in result["events"])


def test_fault_action_follow_up_uses_configured_detection_time(monkeypatch) -> None:
    created = {}

    class FakeTask:
        def done(self):
            return False

        def cancel(self):
            created["cancelled"] = True

    def fake_create_task(coro):
        created["coro"] = coro
        coro.close()
        return FakeTask()

    monkeypatch.setattr(webapp.asyncio, "create_task", fake_create_task)
    webapp._state["ai_event_refresh_tasks"] = {}
    result = webapp._schedule_ai_event_follow_up(
        {"id": "ai-t4", "kind": "ai"},
        fault_run_id="fault-1",
        expected_detection_time_s=30,
    )

    assert result["scheduled"] is True
    assert result["delay_s"] == 30
    assert result["task_id"] in webapp._state["ai_event_refresh_tasks"]


def test_ai_event_refresh_confirms_then_auto_resolves(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    cfg = webapp._get_config()
    old_confirmation = cfg.detection.confirmation_points
    cfg.detection.confirmation_points = 2
    try:
        first = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())
        merged = webapp._merge_ai_events_refresh(first, cluster_ids=["ai-t4"])
        latency = next(event for event in merged if event["event_type"] == "inference_latency_high")
        assert latency["status"] == "observing"
        assert latency["confirmations"] == 1
        assert latency["required_confirmations"] == 2

        second = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())
        merged = webapp._merge_ai_events_refresh(second, cluster_ids=["ai-t4"])
        latency = next(event for event in merged if event["event_type"] == "inference_latency_high")
        assert latency["status"] == "active"
        assert latency["confirmations"] == 2

        merged = webapp._merge_ai_events_refresh([], cluster_ids=["ai-t4"])
        latency = next(event for event in merged if event["event_type"] == "inference_latency_high")
        assert latency["status"] == "resolved"
        assert "自动恢复" in latency["resolution"]
    finally:
        cfg.detection.confirmation_points = old_confirmation


def test_ai_event_refresh_attaches_fault_context_and_resolves_diagnosed_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    monkeypatch.setattr(webapp, "_FAULT_RUNS_FILE", tmp_path / "fault_runs.json")
    now = 1782973300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    webapp._append_fault_run({
        "id": "fault-exp-1",
        "started_at": now - 30,
        "finished_at": now - 20,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "experiment",
        "scenario_id": "llm-sw-queue-overflow",
        "fault_type": "request_queue_overflow",
        "layer": "software",
    })
    events = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())
    merged = webapp._merge_ai_events_refresh(events, cluster_ids=["ai-t4"])
    latency = next(event for event in merged if event["event_type"] == "inference_latency_high")
    assert latency["evaluation_context"]["scenario_id"] == "llm-sw-queue-overflow"
    assert not any(item.get("source") == "fault_experiment" for item in latency["evidence"])
    assert "llm-sw-queue-overflow" not in str(webapp._ai_event_to_skill_payload(latency))

    latency["status"] = "diagnosed"
    webapp._upsert_ai_event(latency)
    webapp._append_fault_run({
        "id": "fault-clean-1",
        "started_at": now - 5,
        "finished_at": now - 1,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "cleanup",
        "scenario_id": "llm-sw-queue-overflow",
        "fault_type": "request_queue_overflow",
        "layer": "software",
    })
    merged = webapp._merge_ai_events_refresh([], cluster_ids=["ai-t4"])
    latency = next(event for event in merged if event["event_type"] == "inference_latency_high")
    assert latency["status"] == "resolved"
    assert "fault-clean-1" in latency["resolution"]


def test_ai_event_skill_payload_maps_to_llm_fault_type() -> None:
    event = next(
        event for event in webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())
        if event["event_type"] == "vllm_queue_backlog"
    )
    payload = webapp._ai_event_to_skill_payload(event)

    assert payload["source"] == "ai_event_center"
    assert payload["domain"] == "llm"
    assert payload["fault_type"] == "endpoint_timeout"
    assert payload["namespace"] == "llm-k8s"


def test_ai_event_skill_payload_maps_node_pressure_to_llm_resource_pressure() -> None:
    event = {
        "id": "ai-node-pressure-1",
        "source": "ai_event_center",
        "domain": "llm",
        "cluster_id": "t4-vllm-k8s",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "event_type": "node_resource_pressure",
        "title": "智算节点资源压力",
        "description": "dds-127 rootfs usage 86% > 85%",
        "metric": "node_rootfs_usage",
        "value": 86.0,
        "threshold": 85.0,
        "evidence": [
            {"source": "prometheus", "signal": "node", "value": "dds-127"},
            {"source": "prometheus", "signal": "metric", "value": "node_rootfs_usage"},
        ],
    }

    payload = webapp._ai_event_to_skill_payload(event)

    assert payload["fault_type"] == "resource_pressure"
    assert payload["cluster_id"] == "t4-vllm-k8s"
    assert payload["metrics"]["metric"] == "node_rootfs_usage"
    assert payload["metrics"]["threshold"] == 85.0


def test_fault_experiment_rootfs_background_signal_is_not_root_cause_candidate() -> None:
    event = {
        "id": "ai-fault-exp-1",
        "source": "fault_experiment",
        "domain": "llm",
        "cluster_id": "t4-vllm-k8s",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "event_type": "fault_experiment_signal",
        "title": "智算运行异常：推理服务观测信号异常",
        "description": "检测到真实运行窗口内的智算服务健康、请求或指标异常",
        "metric": "fault_run",
        "value": "observable_anomaly",
        "threshold": "diagnose",
        "experiment_run_id": "fault-secret",
        "step_id": "step-secret",
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "running_requests": 0,
            "waiting_requests": 0,
            "node_cpu_usage": 0.5,
            "node_memory_usage": 21.2,
            "node_rootfs_usage": 95.9,
        },
        "evidence": [
            {"source": "observation", "signal": "node_rootfs_usage", "value": 95.9},
        ],
    }

    payload = webapp._ai_event_to_skill_payload(event)

    assert "observation_signals" not in payload
    assert payload["background_observation_signals"][0]["signal"] == "node_rootfs_usage"
    assert "candidate_skills" not in payload
    assert payload["metrics"]["metric"] == "fault_run"
    assert "experiment_run_id" not in payload
    assert "step_id" not in payload
    assert payload["observation_thresholds"]["kv_cache"] > 0


def test_ai_event_adapter_preserves_agent_evidence_calibrated_confidence() -> None:
    result = {
        "skill_runtime": {
            "selection_mode": "agent_llm_exact_skill_calls",
            "primary_skill_id": "llm.model_serving_endpoint",
        },
        "diagnosis": {
            "skill_id": "llm.model_serving_endpoint",
            "root_cause": "Service selector 与 Pod 标签不匹配",
            "confidence_raw": 0.9,
            "confidence": 0.61,
            "confidence_explanation": {
                "method": "evidence_calibrated_v2",
                "calibrated_confidence": 0.61,
                "factors": {"evidence": 0.6},
            },
        },
    }
    normalized = webapp._normalize_ai_event_skill_result(
        {"source": "fault_experiment", "event_type": "fault_experiment_signal", "id": "event-1"},
        {
            "observation": {"endpoint_status": "failed", "metrics_status": "ok"},
            "endpoint_status": "failed",
            "metrics_status": "ok",
            "process_count": 1,
            "metrics_count": 5,
        },
        result,
    )

    diagnosis = normalized["diagnosis"]
    assert diagnosis["confidence"] == 0.61
    assert diagnosis["confidence_explanation"]["method"] == "evidence_calibrated_v2"
    assert diagnosis["confidence_explanation"]["observation_validation"]["method"] == "llm_fault_observable_signals_v1"


def test_node_rootfs_event_corrects_stale_cpu_diagnosis() -> None:
    event = {
        "id": "ai-rootfs-pressure",
        "source": "ai_dashboard",
        "event_type": "node_resource_pressure",
        "metric": "node_resources",
        "value": {"node_rootfs_usage": 95.97},
        "threshold": {"node_rootfs_usage": 85.0},
        "diagnosis": {
            "fault_type": "resource_pressure",
            "root_cause": "vLLM 所在节点存在 CPU 资源压力，可能导致预处理、调度或请求排队变慢",
            "confidence": 0.586,
        },
    }

    compact = webapp._compact_ai_event_for_list(event)

    assert "根分区" in compact["diagnosis"]["root_cause"]
    assert "CPU 资源压力" not in compact["diagnosis"]["root_cause"]
    assert compact["diagnosis"]["raw_skill_root_cause"].startswith("vLLM 所在节点存在 CPU")
    assert compact["diagnosis"]["evidence_alignment"]["signal"] == "node_rootfs_usage"


def test_fault_experiment_node_resource_signals_need_window_causality(monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_ai_event_live_observation", lambda _payload: {})
    for signal_name in ("node_cpu_usage", "node_memory_usage", "node_rootfs_usage"):
        event = {
            "id": f"ai-background-{signal_name}",
            "source": "fault_experiment",
            "domain": "llm",
            "cluster_id": "ai-cluster",
            "namespace": "inference",
            "deployment": "model-server",
            "service": "model-server",
            "event_type": "fault_experiment_signal",
            "title": "智算运行异常",
            "description": "当前值超过阈值，但缺少故障窗口内的变化证据",
            "metric": "fault_run",
            "value": "observable_anomaly",
            "threshold": "diagnose",
            "observation": {signal_name: 96.0},
            "evidence": [{"source": "observation", "signal": signal_name, "value": 96.0}],
        }

        payload = webapp._ai_event_to_skill_payload(event)

        assert signal_name in {
            item["signal"] for item in payload.get("background_observation_signals") or []
        }
        assert signal_name not in {
            item["signal"] for item in payload.get("observation_signals") or []
        }


def test_fault_experiment_node_resource_delta_becomes_foreground_signal(monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_ai_event_live_observation", lambda _payload: {})
    event = {
        "id": "ai-window-node-pressure",
        "source": "fault_experiment",
        "domain": "llm",
        "cluster_id": "ai-cluster",
        "namespace": "inference",
        "deployment": "model-server",
        "service": "model-server",
        "event_type": "fault_experiment_signal",
        "title": "智算运行异常",
        "description": "节点 CPU 在故障窗口内跨越阈值",
        "metric": "fault_run",
        "value": "observable_anomaly",
        "threshold": "diagnose",
        "observation": {
            "node_cpu_usage": 93.0,
            "metric_baseline": {"node_cpu_usage": 18.0},
            "metric_fault": {"node_cpu_usage": 93.0},
            "metric_deltas": {"node_cpu_usage": 75.0},
        },
        "evidence": [{"source": "observation", "signal": "node_cpu_usage", "value": 93.0}],
    }

    payload = webapp._ai_event_to_skill_payload(event)

    assert "background_observation_signals" not in payload
    assert {item["signal"] for item in payload["observation_signals"]} == {"node_cpu_usage"}
    assert payload["candidate_skills"] == ["k8s.resource_pressure"]


def test_fault_run_observation_preserves_metric_window_comparison() -> None:
    observation = webapp._fault_run_observation_payload({
        "result": {
            "metrics_comparison": {
                "baseline": {"node_cpu_usage": 12.0},
                "fault": {"node_cpu_usage": 91.0},
                "delta": {"node_cpu_usage": 79.0},
            },
            "all_snapshots": [],
        }
    })

    assert observation["node_cpu_usage"] == 91.0
    assert observation["metric_baseline"]["node_cpu_usage"] == 12.0
    assert observation["metric_fault"]["node_cpu_usage"] == 91.0
    assert observation["metric_deltas"]["node_cpu_usage"] == 79.0


def test_fault_experiment_slo_degradation_excludes_background_resource_candidate() -> None:
    event = {
        "id": "ai-fault-exp-slo-1",
        "source": "fault_experiment",
        "domain": "llm",
        "cluster_id": "t4-vllm-k8s",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "event_type": "fault_experiment_signal",
        "title": "智算运行异常：推理服务观测信号异常",
        "description": "故障窗口内请求成功率和端到端时延恶化",
        "metric": "fault_run",
        "value": "observable_anomaly",
        "threshold": "diagnose",
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "success_rate": 0.13,
            "e2e_p95_seconds": 4.875,
            "waiting_requests": 0,
            "node_rootfs_usage": 95.9,
        },
        "evidence": [
            {"source": "observation", "signal": "success_rate", "value": 0.13},
            {"source": "observation", "signal": "e2e_p95_seconds", "value": 4.875},
            {"source": "observation", "signal": "node_rootfs_usage", "value": 95.9},
        ],
    }

    payload = webapp._ai_event_to_skill_payload(event)

    assert {item["signal"] for item in payload["background_observation_signals"]} == {"node_rootfs_usage"}
    assert {item["signal"] for item in payload["observation_signals"]} == {"success_rate", "e2e_p95_seconds"}
    assert payload["candidate_skills"] == ["llm.business_slo"]
    assert "k8s.resource_pressure" not in payload["candidate_skills"]
    assert "llm.vllm_scheduler_queue" not in payload["candidate_skills"]


def test_json_safe_snapshot_sanitizes_non_finite_runtime_values() -> None:
    snapshot = webapp._json_safe_snapshot({
        "confidence": float("nan"),
        "metrics": [float("inf"), float("-inf"), 1.0],
    })

    assert snapshot == {"confidence": None, "metrics": [None, None, 1.0]}
    json.dumps(snapshot, allow_nan=False)
    assert webapp.app.router.default_response_class is webapp._SafeJSONResponse
    response = webapp._SafeJSONResponse({"metric": float("nan")})
    assert json.loads(response.body) == {"metric": None}


def test_live_observation_keeps_success_throughput_distinct_from_success_ratio(monkeypatch) -> None:
    def fake_kubectl(command, **_kwargs):
        if "get deploy/" in command:
            return {
                "ok": True,
                "json": {
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "vllm-server"}}},
                    "status": {"readyReplicas": 1, "availableReplicas": 1},
                },
            }
        if "get pods" in command:
            return {"ok": True, "json": {"items": []}}
        if "get endpoints/" in command:
            return {"ok": True, "json": {"subsets": [{"addresses": [{"ip": "10.0.0.8"}]}]}}
        return {"ok": False, "returncode": 1}

    queries = []

    def fake_prom_value(_base_url, query):
        queries.append(query)
        return 0.25 if "request_success_total" in query else None

    monkeypatch.setattr(webapp, "_kubectl_heal_check", fake_kubectl)
    monkeypatch.setattr(webapp, "_find_k8s_cluster", lambda _cluster_id: {"id": "ai-t4"})
    monkeypatch.setattr(webapp, "_reachable_cluster_prometheus_url", lambda *_args: "http://prometheus")
    monkeypatch.setattr(webapp, "_prometheus_api_ok", lambda _url: True)
    monkeypatch.setattr(webapp, "_prom_instant_first_value", fake_prom_value)

    observation = webapp._ai_event_live_observation({
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
    })

    assert observation["success_rps"] == 0.25
    assert "success_rate" not in observation
    assert any("request_success_total" in query for query in queries)


def test_live_observation_records_workload_recreation_inside_observation_window(monkeypatch) -> None:
    monkeypatch.setattr(webapp.time, "time", lambda: 1000.0)

    def fake_kubectl(command, **_kwargs):
        if "get deploy/" in command:
            return {
                "ok": True,
                "json": {
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "vllm-server"}}},
                    "status": {"readyReplicas": 1, "availableReplicas": 1},
                },
            }
        if "get pods" in command:
            return {
                "ok": True,
                "json": {
                    "items": [{
                        "metadata": {"name": "vllm-server-new", "creationTimestamp": "1970-01-01T00:16:35Z"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "vllm", "restartCount": 0, "state": {"running": {}}}],
                        },
                    }],
                },
            }
        if "get endpoints/" in command:
            return {"ok": True, "json": {"subsets": [{"addresses": [{"ip": "10.0.0.8"}]}]}}
        return {"ok": False, "returncode": 1}

    monkeypatch.setattr(webapp, "_kubectl_heal_check", fake_kubectl)
    monkeypatch.setattr(webapp, "_find_k8s_cluster", lambda _cluster_id: {"id": "ai-t4"})
    monkeypatch.setattr(webapp, "_reachable_cluster_prometheus_url", lambda *_args: "")

    observation = webapp._ai_event_live_observation({
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "observation_window_started_at": 990.0,
    })

    assert observation["pod_count"] == 1
    assert observation["pod_recreated_in_window"] is True
    assert observation["pod_age_seconds"] == 5.0


def test_live_observation_collects_failed_container_state_and_bounded_logs(monkeypatch) -> None:
    monkeypatch.setattr(webapp.time, "time", lambda: 1000.0)

    def fake_kubectl(command, **_kwargs):
        if "get deploy/" in command:
            return {
                "ok": True,
                "json": {
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"workload": "model-server"}}},
                    "status": {"readyReplicas": 0, "availableReplicas": 0},
                },
            }
        if "get pods" in command:
            return {
                "ok": True,
                "json": {
                    "items": [{
                        "metadata": {"name": "model-server-new", "creationTimestamp": "1970-01-01T00:16:35Z"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{
                                "name": "server",
                                "restartCount": 1,
                                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off restarting"}},
                                "lastState": {"terminated": {
                                    "reason": "Error",
                                    "exitCode": 1,
                                    "finishedAt": "1970-01-01T00:16:38Z",
                                }},
                            }],
                        },
                    }],
                },
            }
        if " logs " in f" {command} ":
            return {"ok": True, "stdout": "RuntimeError: Failed to infer device type"}
        if "get endpoints/" in command:
            return {"ok": True, "json": {"subsets": []}}
        return {"ok": False, "returncode": 1}

    monkeypatch.setattr(webapp, "_kubectl_heal_check", fake_kubectl)
    monkeypatch.setattr(webapp, "_find_k8s_cluster", lambda _cluster_id: {"id": "cluster-a"})
    monkeypatch.setattr(webapp, "_reachable_cluster_prometheus_url", lambda *_args: "")

    observation = webapp._ai_event_live_observation({
        "cluster_id": "cluster-a",
        "namespace": "inference",
        "deployment": "model-server",
        "service": "model-server",
        "observation_window_started_at": 990.0,
    })

    assert observation["container_waiting_reasons"] == ["CrashLoopBackOff"]
    assert observation["container_terminated_in_window"] is True
    assert observation["last_exit_code"] == 1
    assert "Failed to infer device type" in observation["container_log_excerpt"]
    assert len(observation["container_log_excerpt"]) <= 6000


def test_live_observation_collects_only_accelerator_visibility_env(monkeypatch) -> None:
    def fake_kubectl(command, **_kwargs):
        if "get deploy/" in command:
            return {
                "ok": True,
                "json": {
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"workload": "model-server"}},
                        "template": {"spec": {"containers": [{
                            "name": "server",
                            "env": [
                                {"name": "CUDA_VISIBLE_DEVICES", "value": "-1"},
                                {"name": "SECRET_TOKEN", "value": "must-not-leak"},
                            ],
                            "resources": {"limits": {"vendor.example/gpu": "2"}},
                        }]}},
                    },
                    "status": {"readyReplicas": 0, "availableReplicas": 0},
                },
            }
        if "get pods" in command:
            return {"ok": True, "json": {"items": []}}
        if "get endpoints/" in command:
            return {"ok": True, "json": {"subsets": []}}
        return {"ok": False, "returncode": 1}

    monkeypatch.setattr(webapp, "_kubectl_heal_check", fake_kubectl)
    monkeypatch.setattr(webapp, "_find_k8s_cluster", lambda _cluster_id: {"id": "cluster-a"})
    monkeypatch.setattr(webapp, "_reachable_cluster_prometheus_url", lambda *_args: "")

    observation = webapp._ai_event_live_observation({
        "cluster_id": "cluster-a",
        "namespace": "inference",
        "deployment": "model-server",
        "service": "model-server",
    })

    assert observation["accelerator_visibility"] == {"server:CUDA_VISIBLE_DEVICES": "-1"}
    assert observation["accelerator_resource_limits"] == {"vendor.example/gpu": 2.0}
    assert "SECRET_TOKEN" not in str(observation)
    signals = webapp._ai_event_observation_signals(observation)
    assert any(item.get("candidate_skill") == "llm.cuda_driver_failure" for item in signals)


def test_ai_fault_experiment_labels_are_hidden_from_diagnosis_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_FAULT_RUNS_FILE", tmp_path / "fault_runs.json")
    now = 1782973300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    webapp._append_fault_run({
        "id": "fault-exp-ai-1",
        "started_at": now - 20,
        "finished_at": now - 5,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "experiment",
        "scenario_id": "llm-sw-queue-overflow",
        "fault_type": "request_queue_overflow",
        "layer": "software",
    })

    events = webapp._generate_ai_events_from_fault_runs({"id": "ai-t4", "default_namespace": "llm-k8s"})
    event = events[0]
    payload = webapp._ai_event_to_skill_payload(event)
    compact = webapp._compact_ai_event_for_list(event)
    query = webapp._ai_event_rca_query(event, payload)
    serialized_payload = str(payload)
    serialized_compact = str(compact)

    assert event["fault_type"] == "fault_experiment_signal"
    assert event["heal_fault_type"] == "ResourceExhausted"
    assert event["evaluation_context"]["fault_type"] == "request_queue_overflow"
    assert payload["fault_type"] == "fault_experiment_signal"
    assert payload["raw_fault_type"] == "fault_experiment_signal"
    assert "experiment_fault_type" not in payload
    assert "experiment_context" not in payload
    assert "scenario" not in payload
    assert "llm-sw-queue-overflow" not in serialized_payload
    assert "request_queue_overflow" not in serialized_payload
    assert "llm-sw-queue-overflow" not in serialized_compact
    assert "request_queue_overflow" not in serialized_compact
    assert "llm-sw-queue-overflow" not in query
    assert "request_queue_overflow" not in query


def test_ai_event_refresh_ingests_k8s_warning_events(monkeypatch) -> None:
    now = 1782973300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }

    async def fake_kubectl_json(cmd: str, namespace: str = "", cluster_id: str = "") -> dict:
        assert cluster_id == "ai-t4"
        if "get pods" in cmd:
            return {
                "items": [
                    {
                        "metadata": {
                            "namespace": "llm-k8s",
                            "name": "vllm-server-abc",
                        }
                    }
                ]
            }
        assert "get events" in cmd
        assert namespace in {"llm-k8s", ""}
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: 2 Insufficient nvidia.com/gpu.",
                    "count": 3,
                    "lastTimestamp": "2026-07-02T06:21:40Z",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "vllm-server-abc",
                        "namespace": "llm-k8s",
                    },
                }
            ]
        }

    monkeypatch.setattr(webapp, "_kubectl_json", fake_kubectl_json)

    events = asyncio.run(webapp._generate_ai_events_from_k8s_events(cluster))

    assert len(events) == 1
    event = events[0]
    assert event["source"] == "k8s_event"
    assert event["namespace"] == "llm-k8s"
    assert event["k8s_reason"] == "FailedScheduling"
    assert event["k8s_object_kind"] == "Pod"
    assert event["fault_type"] == "scheduling_failed"
    assert event["severity"] == "critical"
    assert event["title"] == "K8s Event: FailedScheduling"
    assert any(item["source"] == "k8s_event" and item["signal"] == "reason" for item in event["evidence"])

    payload = webapp._ai_event_to_skill_payload(event)
    assert payload["fault_type"] == "scheduling_failed"
    assert payload["k8s_event"]["object_name"] == "vllm-server-abc"


def test_ai_event_k8s_source_probe_reports_connection_state(monkeypatch) -> None:
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "kubeconfig": "/tmp/t4.conf",
        "default_namespace": "llm-k8s",
    }

    async def fake_kubectl_json(cmd: str, namespace: str = "", cluster_id: str = "") -> dict:
        assert cluster_id == "ai-t4"
        if "get nodes" in cmd:
            return {
                "items": [
                    {"metadata": {"name": "dds-127"}},
                    {"metadata": {"name": "dds-128"}},
                ]
            }
        assert "get events" in cmd
        assert namespace == "llm-k8s"
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: 2 Insufficient nvidia.com/gpu.",
                    "lastTimestamp": "2026-07-02T06:21:40Z",
                }
            ]
        }

    monkeypatch.setattr(webapp, "_kubectl_json", fake_kubectl_json)

    probe = asyncio.run(webapp._probe_ai_k8s_event_source(cluster))

    assert probe["configured"] is True
    assert probe["available"] is True
    assert probe["nodes"] == ["dds-127", "dds-128"]
    assert probe["event_count"] == 1
    assert probe["warning_count"] == 1
    assert probe["recent_reasons"] == ["FailedScheduling"]
    assert "已连接" in probe["message"]


def test_ai_event_k8s_source_probe_reports_missing_kubeconfig() -> None:
    probe = asyncio.run(webapp._probe_ai_k8s_event_source({
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
    }))

    assert probe["configured"] is False
    assert probe["available"] is False
    assert "未配置 kubeconfig" in probe["message"]


def test_ai_event_refresh_ignores_stale_k8s_warning_events(monkeypatch) -> None:
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    now = 1783330000.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)

    async def fake_kubectl_json(cmd: str, namespace: str = "", cluster_id: str = "") -> dict:
        if "get pods" in cmd:
            return {"items": []}
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: 1 Insufficient nvidia.com/gpu.",
                    "lastTimestamp": "2026-07-06T08:00:00Z",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "vllm-server-old",
                        "namespace": "llm-k8s",
                    },
                }
            ]
        }

    monkeypatch.setattr(webapp, "_kubectl_json", fake_kubectl_json)

    assert asyncio.run(webapp._generate_ai_events_from_k8s_events(cluster)) == []


def test_ai_event_refresh_ignores_deleted_pod_k8s_warning_events(monkeypatch) -> None:
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    monkeypatch.setattr(webapp.time, "time", lambda: 1783330000.0)

    async def fake_kubectl_json(cmd: str, namespace: str = "", cluster_id: str = "") -> dict:
        if "get pods" in cmd:
            return {
                "items": [
                    {
                        "metadata": {
                            "namespace": "llm-k8s",
                            "name": "vllm-server-running",
                        }
                    }
                ]
            }
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: 1 Insufficient nvidia.com/gpu.",
                    "lastTimestamp": "2026-07-06T10:45:00Z",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "vllm-server-deleted",
                        "namespace": "llm-k8s",
                    },
                }
            ]
        }

    monkeypatch.setattr(webapp, "_kubectl_json", fake_kubectl_json)

    assert asyncio.run(webapp._generate_ai_events_from_k8s_events(cluster)) == []


def test_ai_event_refresh_ignores_recovered_pod_scheduling_warning(monkeypatch) -> None:
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    monkeypatch.setattr(webapp.time, "time", lambda: 1783330000.0)

    async def fake_kubectl_json(cmd: str, namespace: str = "", cluster_id: str = "") -> dict:
        if "get pods" in cmd:
            return {
                "items": [
                    {
                        "metadata": {
                            "namespace": "llm-k8s",
                            "name": "vllm-server-running",
                        },
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                ]
            }
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: 1 Insufficient nvidia.com/gpu.",
                    "lastTimestamp": "2026-07-06T10:45:00Z",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "vllm-server-running",
                        "namespace": "llm-k8s",
                    },
                }
            ]
        }

    monkeypatch.setattr(webapp, "_kubectl_json", fake_kubectl_json)

    assert asyncio.run(webapp._generate_ai_events_from_k8s_events(cluster)) == []


def test_recent_llm_inject_run_generates_resolvable_ai_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    monkeypatch.setattr(webapp, "_FAULT_RUNS_FILE", tmp_path / "fault_runs.json")
    now = 1782974300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    webapp._append_fault_run({
        "id": "fault-inject-1",
        "started_at": now - 20,
        "finished_at": now - 18,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "inject",
        "scenario_id": "sw-001",
        "fault_type": "process_crash",
        "layer": "software",
    })

    generated = webapp._generate_ai_events_from_fault_runs(cluster)
    assert len(generated) == 1
    assert generated[0]["event_type"] == "fault_experiment_signal"
    assert generated[0]["source"] == "fault_experiment"
    assert generated[0]["status"] == "active"
    payload = webapp._ai_event_to_skill_payload(generated[0])
    assert payload["fault_type"] == "fault_experiment_signal"
    assert "process_crash" not in str(payload)

    merged = webapp._merge_ai_events_refresh(generated, cluster_ids=["ai-t4"])
    event = next(item for item in merged if item["event_type"] == "fault_experiment_signal")
    assert event["status"] == "active"
    assert event["required_confirmations"] == 1
    assert event["evaluation_context"]["scenario_id"] == "sw-001"

    webapp._append_fault_run({
        "id": "fault-clean-2",
        "started_at": now - 5,
        "finished_at": now - 1,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "cleanup",
        "scenario_id": "sw-001",
        "fault_type": "process_crash",
        "layer": "software",
    })
    merged = webapp._merge_ai_events_refresh([], cluster_ids=["ai-t4"])
    event = next(item for item in merged if item["event_type"] == "fault_experiment_signal")
    assert event["status"] == "resolved"
    assert "fault-clean-2" in event["resolution"]


def test_fault_event_live_observation_overrides_injection_snapshot(monkeypatch) -> None:
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    run = {
        "id": "fault-kv-1",
        "started_at": 100.0,
        "finished_at": 110.0,
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "inject",
        "scenario_id": "sw-003",
        "fault_type": "kv_cache_exhaustion",
        "layer": "software",
        "observation": {
            "metrics_status": "ok",
            "kv_cache_usage": 59.4,
            "waiting_requests": 36,
        },
    }
    updated = {}
    monkeypatch.setattr(webapp, "_active_ai_fault_runs", lambda *_args, **_kwargs: [run])
    monkeypatch.setattr(webapp, "_ai_event_live_observation", lambda _payload: {
        "metrics_status": "ok",
        "kv_cache_usage": 98.8,
        "kv_cache_usage_current": 98.8,
        "waiting_requests": 42,
        "metrics_target": {"scope": "target", "selector": 'instance="vllm-server"'},
        "observation_context": {
            "signals": {"kv_cache_usage": {"incident": {"max": 98.8}}},
        },
    })
    monkeypatch.setattr(
        webapp,
        "_update_fault_run_observation_extrema",
        lambda run_id, observation: updated.update({"run_id": run_id, "observation": observation}),
    )

    event = webapp._generate_ai_events_from_fault_runs(cluster)[0]

    assert event["observation"]["kv_cache_usage"] == 98.8
    assert event["observation"]["waiting_requests"] == 42
    assert updated["observation"]["kv_cache_usage"] == 98.8
    assert updated["observation"]["metrics_target"]["scope"] == "target"


def test_recent_llm_experiment_run_generates_diagnosable_ai_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    monkeypatch.setattr(webapp, "_FAULT_RUNS_FILE", tmp_path / "fault_runs.json")
    now = 1782974300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    webapp._append_fault_run({
        "id": "fault-exp-1",
        "started_at": now - 30,
        "finished_at": now - 10,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "experiment",
        "scenario_id": "sw-001",
        "fault_type": "process_crash",
        "layer": "software",
    })

    generated = webapp._generate_ai_events_from_fault_runs(cluster)

    assert len(generated) == 1
    assert generated[0]["source"] == "fault_experiment"
    assert generated[0]["event_type"] == "fault_experiment_signal"
    assert generated[0]["fault_type"] == "fault_experiment_signal"
    assert generated[0]["evaluation_context"]["action"] == "experiment"


def test_recent_llm_experiment_event_carries_observation_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_FAULT_RUNS_FILE", tmp_path / "fault_runs.json")
    now = 1782974300.0
    monkeypatch.setattr(webapp.time, "time", lambda: now)
    cluster = {
        "id": "ai-t4",
        "name": "T4 vLLM",
        "default_namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen",
    }
    webapp._append_fault_run({
        "id": "fault-exp-observed",
        "started_at": now - 30,
        "finished_at": now - 10,
        "status": "executed",
        "target_mode": "k8s",
        "cluster_id": "ai-t4",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "action": "experiment",
        "scenario_id": "sw-001",
        "fault_type": "process_crash",
        "layer": "software",
        "loadgen": {
            "model": "qwen2.5-coder-3b-k8s",
            "results": [
                {"stdout": '{"requests": 20, "success": 18, "errors": 2, "rps": 0.4, "latency_p95_s": 1.2}'},
            ],
        },
        "result": {
            "injection": {"success": True},
            "recovery": [{"layer": "software", "success": True}],
            "all_snapshots": [
                {
                    "active_requests": 3,
                    "throughput_rps": 0.4,
                    "gpu_utilization": 32,
                    "metrics": {'vllm:num_requests_waiting{model_name="qwen"}': 4},
                }
            ],
            "metrics_comparison": {"vllm:num_requests_running": {"before": 0, "during": 2}},
        },
    })

    event = webapp._generate_ai_events_from_fault_runs(cluster)[0]
    payload = webapp._ai_event_to_skill_payload(event)

    assert event["observation"]["metrics_status"] == "ok"
    assert event["observation"]["loadgen_success"] == 18
    assert event["observation"]["running_requests"] == 3
    assert event["observation"]["waiting_requests"] == 4
    assert payload["observation"]["loadgen_model"] == "qwen2.5-coder-3b-k8s"
    assert payload["metrics_status"] == "ok"
    assert payload["loadgen_errors"] == 2
    assert payload["recovery_success"] is True
    compact = webapp._compact_ai_event_for_list(event)
    assert compact["observation"]["metrics_status"] == "ok"
    assert compact["observation"]["loadgen_success"] == 18


def test_ai_event_skill_workflow_triggers_agent_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    monkeypatch.setattr(webapp, "_SKILL_WORKFLOW_RUNS_FILE", tmp_path / "skill_runs.json")
    monkeypatch.setattr(webapp, "_SKILL_WORKFLOW_ARTIFACT_DIR", tmp_path / "artifacts")

    captured: dict = {}

    async def fake_rca_run(request) -> dict:
        captured.update(await request.json())
        return {"run_id": "rca-agent-skill-001", "status": "running"}

    monkeypatch.setattr(webapp, "rca_run", fake_rca_run)
    event = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0]
    saved = webapp._upsert_ai_event(event)

    client = TestClient(webapp.app)
    response = client.post(f"/api/ai-events/{saved['id']}/skill", json={"stages": ["detect", "diagnose"]})

    assert response.status_code == 200
    data = response.json()
    assert data["event"]["status"] == "diagnosed"
    assert data["agent_run"]["run_id"] == "rca-agent-skill-001"
    assert data["event"]["agent_run"]["mode"] == "agent_skill"
    assert data["event"]["agent_runs"]["agent_skill"]["run_id"] == "rca-agent-skill-001"
    assert data["event"]["agent_run"]["status"] == "running"
    assert data["event"]["agent_run"]["requested_stages"] == ["detect", "diagnose"]
    assert "上下文检索、假设生成、验证规划、调查取证、推理裁决" in captured["query"]
    assert captured["namespace"] == "llm-k8s"
    assert captured["skill_workflow"]["domain"] == "llm"
    assert captured["skill_workflow"]["stages"] == ["detect", "diagnose"]
    assert captured["skill_workflow"]["analysis_phases"] == ["context", "hypothesis", "plan", "investigation", "reasoning"]
    assert captured["skill_workflow"]["llm_timeout_s"] == 60
    assert captured["skill_workflow"]["payload"]["fault_type"]
    assert captured["dedup_key"].startswith(f"ai-event:{saved['id']}:agent-skill:")
    assert webapp._load_skill_workflow_runs() == []


def test_ai_event_workflow_start_defers_live_observation(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(webapp, "_ai_event_live_observation", lambda payload: calls.append(payload) or {"waiting_requests": 9})
    event = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0]

    payload = webapp._ai_event_to_skill_payload(event, include_live_observation=False)

    assert calls == []
    assert "live_observation" not in payload


def test_ai_event_agent_workflow_reuses_multi_agent_rca(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")

    captured: dict = {}

    async def fake_rca_run(request) -> dict:
        captured.update(await request.json())
        return {"run_id": "rca-multi-agent-001", "status": "running"}

    monkeypatch.setattr(webapp, "rca_run", fake_rca_run)
    event = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0]
    saved = webapp._upsert_ai_event(event)

    client = TestClient(webapp.app)
    response = client.post(f"/api/ai-events/{saved['id']}/agent", json={"mode": "analysis"})

    assert response.status_code == 200
    data = response.json()
    assert data["event"]["status"] == "diagnosed"
    assert data["agent_run"]["run_id"] == "rca-multi-agent-001"
    assert data["agent_run"]["mode"] == "multi_agent"
    assert data["event"]["agent_runs"]["multi_agent"]["run_id"] == "rca-multi-agent-001"
    assert "skill_workflow" not in captured
    assert "请输出中文结论" in captured["query"]
    assert captured["namespace"] == "llm-k8s"
    assert captured["dedup_key"].startswith(f"ai-event:{saved['id']}:multi-agent:analysis:")


def test_ai_event_workflows_accept_analysis_phases_without_hypothesis(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    requests = []

    async def fake_rca_run(request) -> dict:
        requests.append(await request.json())
        return {"run_id": f"rca-config-{len(requests)}", "status": "running"}

    monkeypatch.setattr(webapp, "rca_run", fake_rca_run)
    saved = webapp._upsert_ai_event(webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0])
    phases = ["context", "plan", "investigation", "reasoning"]
    client = TestClient(webapp.app)

    multi = client.post(f"/api/ai-events/{saved['id']}/agent", json={"analysis_phases": phases})
    skill = client.post(
        f"/api/ai-events/{saved['id']}/skill",
        json={"stages": ["detect", "diagnose"], "analysis_phases": phases},
    )

    assert multi.status_code == 200
    assert skill.status_code == 200
    assert multi.json()["agent_run"]["analysis_phases"] == phases
    assert skill.json()["agent_run"]["analysis_phases"] == phases
    assert requests[0]["analysis_phases"] == phases
    assert requests[1]["skill_workflow"]["analysis_phases"] == phases
    assert "假设生成" not in requests[0]["query"].split("本次仅执行已选分析阶段：", 1)[1].split("；", 1)[0]


def test_ai_event_heal_capability_returns_compact_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    event = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0]
    saved = webapp._upsert_ai_event(event)

    client = TestClient(webapp.app)
    response = client.post(f"/api/ai-events/{saved['id']}/heal", json={})

    assert response.status_code == 200
    data = response.json()
    assert data["event"]["status"] == "healing"
    assert data["heal_result"]["source"] == "ai_event_agent_heal"
    assert data["event"]["heal_result"]["summary"]["fault_type"]


def test_ai_event_list_returns_compact_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(webapp, "_AI_EVENTS_FILE", tmp_path / "ai_events.json")
    event = webapp._generate_ai_events_from_dashboard_payload(_dashboard_payload())[0]
    event["skill_result"] = {
        "status": "completed",
        "domain": "llm",
        "dry_run": True,
        "workflow_run_id": "skillwf-big",
        "dashboard_run_id": "dash-big",
        "dashboard_bridge": {"mode": "local_fallback"},
        "skill_runtime": {
            "matched_count": 12,
            "primary_skill_id": "llm.k8s_vllm_observability",
            "runs": [{"payload": "x" * 10000}],
        },
        "legacy": {"payload": "y" * 10000},
        "execution_plan": {"commands": [f"cmd-{i}" for i in range(10)]},
    }
    event["diagnosis"] = {
        "fault_type": "endpoint_timeout",
        "root_cause": "请求排队",
        "confidence": 0.82,
        "evidence": ["large"] * 100,
    }
    webapp._upsert_ai_event(event)

    client = TestClient(webapp.app)
    response = client.get("/api/ai-events?limit=1")

    assert response.status_code == 200
    item = response.json()["events"][0]
    assert item["skill_result"]["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert isinstance(item["agent_runs"], dict)
    assert "runs" not in item["skill_result"]["skill_runtime"]
    assert "legacy" not in item["skill_result"]
    assert len(item["skill_result"]["execution_plan"]["commands"]) == 6
    assert "evidence" not in item["diagnosis"]
