import copy
from datetime import datetime, timezone

import pytest

from agents.alert_agent import Alert
from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_pod_scheduling import merge_pod_scheduling, scheduling_condition


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def inputs():
    labels = dict(__name__="ALERTS", alertname="KubePodNotReady", alertstate="firing",
                  job="kube-state-metrics", severity="critical", pod="api", namespace="shop")
    prom = Alert.from_prometheus_sample(dict(metric=labels, value=[1000.5, "1"]), "", "c")
    event = Alert.from_kubernetes_event(dict(reason="FailedScheduling", type="Warning",
        source={"component":"default-scheduler"}, involvedObject=dict(kind="Pod", name="api", namespace="shop", uid="uid1"),
        lastTimestamp=iso(910), metadata={"uid":"event1"}))
    scan = compress_alerts([vars(prom), vars(event)], cluster_id="c")
    scan.update(origin="live_scan", cluster_id="c", collection_started_at=1000, collection=dict(prometheus="ok",kubectl="ok"))
    group = next(g for g in scan["groups"] if g["representative"] == prom.name)
    measurement = {key:group[key] for key in ("cluster_id", "namespace", "entity_kind", "entity", "alert_ids")}
    measurement.update(status="ready", provenance="live_prometheus_rule", query_basis="reviewed_expression",
                       measurement_adapter={"adapter_id":"KubePodNotReady-v1"}, evidence_id="m-1",
                       points=[[t,1] for t in (910,930,950,970,990)])
    scan["metric_evidence"] = dict(groups={group["group_id"]:measurement},provenance="live_prometheus_rule")
    pod = dict(name="api", namespace="shop", uid="uid1", phase="Pending", node="",
               unschedulable=dict(scheduler="default-scheduler", since=800, created_at=750))
    inventory = dict(cluster_id="c", pods=[pod])
    phase = dict(metric=dict(pod="api",namespace="shop",uid="uid1",phase="Pending",job="kube-state-metrics"),
                 value=[1000.5, "1"])
    vector = {"status":"success","data":{"resultType":"vector","result":[phase]}}
    return scan, inventory, vector


def test_pair_preserves_originals_critical_measurement_and_diagnosis(tmp_path):
    scan, inventory, vector = inputs()
    raw = copy.deepcopy(scan["raw_alerts"])
    assert merge_pod_scheduling(scan, inventory, "http://fixture", lambda *args: vector)
    assert scan["raw_alerts"] == raw and not raw[0]["entity_uid"]
    assert scan["retained_alerts"] == 2 and scan["compressed_groups"] == 1 and scan["compression_ratio"] == .5
    group, = scan["groups"]
    assert group["entity_uid"] == "uid1" and group["severity"] == "critical"
    assert group["event_merge"]["scheduling"]["event_alert_id"] == raw[1]["alert_id"]
    assert scan["metric_evidence"]["groups"][group["group_id"]]["measurement_alert_ids"] == [raw[0]["alert_id"]]
    assert not merge_pod_scheduling(scan, inventory, "http://fixture", lambda *args: pytest.fail("duplicate read"))
    store = CompressionStore(tmp_path)
    saved = store.create(scan)
    dossier = store.dossier(saved["scan_id"], group["group_id"], 1, for_diagnosis=True)
    assert len(dossier["event_merge"]["original_group_ids"]) == 2 and dossier["raw_alert_ids_total"] == 2
    split = store.edit(saved["scan_id"], revision=1, action="split", group_ids=[group["group_id"]],
                       alert_ids=[raw[1]["alert_id"]], reason="inspect sources")
    assert split["compressed_groups"] == 2 and split["scheduling_compression"]["status"] == "invalidated_by_edit"


@pytest.mark.parametrize("case", [
    "import", "degraded", "partial", "cluster", "wrong_instance", "duplicate_inventory", "assigned", "recovered",
    "different_scheduler", "new_episode", "stale_event", "unbound_event", "subobject", "unknown_metric", "other_rule",
    "zero_operand", "different_rule_membership", "missing_phase_uid", "other_phase_uid", "other_cluster_label",
    "duplicate_series", "partial_vector", "wrong_time", "zero_phase", "wrong_namespace", "matrix", "limit",
    "broken_envelope", "broken_data", "missing_value", "invalid_label",
])
def test_pair_requires_complete_independent_evidence(case):
    scan, inventory, vector = inputs()
    prom, event = scan["raw_alerts"]
    pod = inventory["pods"][0]
    metric = next(iter(scan["metric_evidence"]["groups"].values()))
    series = vector["data"]["result"][0]
    if case == "import": scan["origin"] = "import"
    if case == "degraded": scan["degraded"] = True
    if case == "partial": scan["collection"]["kubectl"] = "partial"
    if case == "cluster": inventory["cluster_id"] = "other"
    if case == "wrong_instance": pod["uid"] = "new"
    if case == "duplicate_inventory": inventory["pods"].append(copy.deepcopy(pod))
    if case == "assigned": pod["node"] = "node1"
    if case == "recovered": pod["phase"] = "Running"
    if case == "different_scheduler": pod["unschedulable"]["scheduler"] = "other"
    if case == "new_episode": pod["unschedulable"]["since"] = 980
    if case == "stale_event": event["timestamp"] = 650
    if case == "unbound_event": event["raw"]["raw"]["involvedObject"].pop("uid")
    if case == "subobject": event["raw"]["raw"]["involvedObject"]["fieldPath"] = "spec.containers{api}"
    if case == "unknown_metric": metric["status"] = "unknown"
    if case == "other_rule": metric["measurement_adapter"]["adapter_id"] = "custom"
    if case == "zero_operand": metric["points"][-1][1] = 0
    if case == "different_rule_membership": metric["alert_ids"] = []
    if case == "missing_phase_uid": series["metric"].pop("uid")
    if case == "other_phase_uid": series["metric"]["uid"] = "other"
    if case == "other_cluster_label": series["metric"]["cluster"] = "other"
    if case == "duplicate_series": vector["data"]["result"].append(copy.deepcopy(series))
    if case == "partial_vector": vector["warnings"] = ["partial"]
    if case == "wrong_time": series["value"][0] -= 1
    if case == "zero_phase": series["value"][1] = "0"
    if case == "wrong_namespace": series["metric"]["namespace"] = "other"
    if case == "matrix": vector["data"]["resultType"] = "matrix"
    if case == "limit": vector["data"]["result"] *= 1001
    if case == "broken_envelope": vector = None
    if case == "broken_data": vector["data"] = "invalid"
    if case == "missing_value": series.pop("value")
    if case == "invalid_label": series["metric"]["namespace"] = []
    before = copy.deepcopy(scan)
    assert not merge_pod_scheduling(scan, inventory, "http://fixture", lambda *args: vector)
    assert scan == before


def test_condition_from_pod_api_requires_one_current_scheduling_failure():
    condition = {"type":"PodScheduled", "status":"False","reason":"Unschedulable","lastTransitionTime":iso(920)}
    pod = dict(metadata={"creationTimestamp":iso(900)},spec={"schedulerName":"default-scheduler"},
               status={"phase":"Pending","conditions":[condition]})
    assert scheduling_condition(pod) == dict(since=920, scheduler="default-scheduler", created_at=900)
    for delta in ({"phase":"Running"}, {"conditions":[condition,condition]}, {"conditions":[]},
                  {"conditions":[dict(condition,status="True")]}, {"conditions":[dict(condition,lastTransitionTime=iso(899))]}):
        assert scheduling_condition(dict(pod,status=dict(pod["status"],**delta))) is None
    assert scheduling_condition(dict(pod,metadata=dict(pod["metadata"],deletionTimestamp=iso(930)))) is None
