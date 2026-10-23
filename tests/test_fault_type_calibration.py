"""Regression tests for specific fault states outranking downstream symptoms."""

from orchestrator.collaboration_optimizer import CollaborationOptimizer


def optimizer() -> CollaborationOptimizer:
    return object.__new__(CollaborationOptimizer)


def test_image_pull_state_outranks_empty_endpoints() -> None:
    mapped = optimizer()._map_fault_type(
        "Pod is in ImagePullBackOff and the deployment has no ready endpoints",
        "ImagePullBackOff",
    )
    assert mapped == "image_pull_error"


def test_scale_zero_remains_service_disruption() -> None:
    mapped = optimizer()._map_fault_type(
        "Deployment spec.replicas=0 and service endpoints empty",
        "replica_zero",
    )
    assert mapped == "service_disruption"


def test_crashloop_state_outranks_empty_endpoints() -> None:
    mapped = optimizer()._map_fault_type(
        "Pod CrashLoopBackOff caused no ready endpoints",
        "pod_unhealthy",
    )
    assert mapped == "application_crash"


def test_unresolved_job_is_not_reclassified_from_rejected_hypotheses():
    text = 'Job Failed; no evidence confirms ImagePullBackOff, OOMKilled or exit code. 不能确认镜像拉取失败。'
    assert optimizer()._map_fault_type(text, 'job_failed') == 'job_failed'


def test_observed_pod_failure_does_not_become_a_missing_exit_diagnosis():
    assert optimizer()._map_fault_type('Pod failed, exit code unavailable', 'pod_failed') == 'pod_failed'


def test_live_target_exit_overrides_contradictory_image_category():
    result = {'root_cause': 'Pod job-pod container probe exited with exit code 7; no image pull evidence.',
              'fault_type': 'image_pull_error', 'affected_services': ['job']}
    evidence = {'event_agent': {'evidence_items': [{'signal_type': 'container_terminated',
        'raw_ref': {'name': 'job-pod', 'exit_code': 7, 'reason': 'Error'}}]}}
    fixed = optimizer().calibrate_fault_type(result, evidence, None)
    assert fixed['fault_type'] == 'application_crash'
    assert fixed['root_cause'] == result['root_cause']
    assert fixed['fault_type_original'] == 'image_pull_error'
    evidence['event_agent']['evidence_items'][0]['raw_ref']['name'] = 'unrelated-pod'
    assert optimizer().calibrate_fault_type(result, evidence, None)['fault_type'] == 'image_pull_error'


def test_mixed_target_states_do_not_force_a_single_exit_category():
    result = {'root_cause': 'Pod job-pod failed', 'fault_type': 'image_pull_error'}
    evidence = {'event_agent': {'evidence_items': [
        {'signal_type': 'container_terminated', 'raw_ref': {'name': 'job-pod', 'exit_code': 7}},
        {'signal_type': 'pod_waiting_state', 'raw_ref': {'name': 'job-pod', 'reason': 'ImagePullBackOff'}}]}}
    assert optimizer().calibrate_fault_type(result, evidence, None)['fault_type'] == 'image_pull_error'
