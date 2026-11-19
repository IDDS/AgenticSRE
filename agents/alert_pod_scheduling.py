"""Join a scheduler Event to a firing Pod rule only with UID-bound live evidence."""
from collections import defaultdict
from datetime import datetime

import requests

from agents.alert_compression import make_group, regroup_stats
from agents.alert_identity import target_identity
from agents.alert_semantics import annotate_groups


PENDING_QUERY = 'kube_pod_status_phase{job="kube-state-metrics",phase="Pending"} == 1'


def scheduling_condition(pod):
    meta, spec, status = pod["metadata"], pod.get("spec", {}), pod.get("status", {})
    if meta.get("deletionTimestamp") or status.get("phase") != "Pending" or spec.get("nodeName"):
        return None
    conditions = [c for c in status.get("conditions", []) if c.get("type") == "PodScheduled"]
    if len(conditions) != 1 or (conditions[0].get("status"), conditions[0].get("reason")) != ("False", "Unschedulable"):
        return None
    try:
        since = datetime.fromisoformat(conditions[0]["lastTransitionTime"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))
        if not since.tzinfo or not created.tzinfo or since < created:
            return None
    except (KeyError, AttributeError, ValueError, OverflowError):
        return None
    return {"since": since.timestamp(), "scheduler": spec.get("schedulerName"), "created_at": created.timestamp()}


def _pending_vector(url, at):
    response = requests.get(url.rstrip("/") + "/api/v1/query", timeout=6,
                            params={"query": PENDING_QUERY, "time": at, "timeout": "4s", "limit": 1001})
    response.raise_for_status()
    return response.json()


def _candidate(rows, groups, inventory, metrics, scan):
    if len(groups) != 2 or len(inventory) != 1 or any(g["alert_count"] != 1 for g in groups):
        return None
    by_name = {g["representative"]: g for g in groups}
    prom, event = by_name.get("KubePodNotReady"), by_name.get("FailedScheduling")
    if not prom or not event:
        return None
    a, e = (rows[g["alert_indices"][0]] for g in (prom, event))
    pod = inventory[0]
    uid, condition = pod.get("uid"), pod.get("unschedulable")
    raw_event = e["raw"].get("raw") or {}
    obj = raw_event.get("involvedObject") or raw_event.get("regarding") or {}
    scheduler = (raw_event.get("reportingController") or raw_event.get("reportingComponent")
                 or (raw_event.get("source") or {}).get("component"))
    if (a["source"] != "prometheus" or a["state"] != "firing" or e["source"] != "k8s_event"
            or raw_event.get("type") != "Warning" or raw_event.get("reason") != "FailedScheduling"
            or (obj.get("kind"), obj.get("namespace"), obj.get("name")) != ("Pod", a["namespace"], a["entity"])
            or obj.get("fieldPath") or not uid or obj.get("uid") != uid or e["entity_uid"] != uid
            or e["identity_status"] == "conflicting" or a["identity_status"] == "conflicting"
            or a["entity_uid"] not in ("", uid) or a["labels"].get("uid", uid) != uid or not condition or not scheduler
            or scheduler != condition["scheduler"] or pod.get("phase") != "Pending" or pod.get("node")):
        return None
    at, event_at = a["timestamp"], e["timestamp"]
    sample = a["raw"].get("raw") or {}
    value = sample.get("value")
    window, start = scan["time_window"], scan["collection_started_at"]
    if (not at or not event_at or not start <= at <= start + window or not at-window <= event_at <= at
            or condition["since"] > event_at or not isinstance(value, list) or len(value) != 2
            or value[0] != at or value[1] != "1" or sample.get("metric") != a["labels"]):
        return None
    metric = metrics.get(prom["group_id"], {})
    points = metric.get("points") or []
    if (metric.get("status") != "ready" or metric.get("provenance") != "live_prometheus_rule"
            or metric.get("query_basis") != "reviewed_expression"
            or (metric.get("measurement_adapter") or {}).get("adapter_id") != "KubePodNotReady-v1"
            or metric.get("alert_ids") != prom["alert_ids"] or not metric.get("evidence_id")
            or any(metric.get(k) != prom[k] for k in ("cluster_id", "namespace", "entity_kind", "entity"))
            or len(points) < 5 or points[-1][1] != 1 or not max(at-window, condition["since"]) <= points[-1][0] <= at):
        return None
    return prom, event, a, uid, condition, metric


def merge_pod_scheduling(scan, inventory, url, fetch=None):
    cluster = scan.get("cluster_id")
    report = scan.get("metric_evidence", {})
    if (not cluster or not inventory or inventory.get("cluster_id") != cluster or not url
            or scan.get("origin") != "live_scan" or scan.get("history") or scan.get("degraded")
            or scan.get("collection") != {"prometheus": "ok", "kubectl": "ok"}
            or report.get("provenance") != "live_prometheus_rule" or not scan.get("collection_started_at")):
        return False
    pods, groups = defaultdict(list), defaultdict(list)
    for pod in inventory.get("pods", []):
        pods[(pod["namespace"], pod["name"])].append(pod)
    for group in scan["groups"]:
        if (group["cluster_id"] == cluster and group["entity_kind"] == "pod" and group["namespace"]
                and group["representative"] in ("KubePodNotReady", "FailedScheduling")):
            groups[(group["namespace"], group["entity"])].append(group)
    rows, metrics = scan["raw_alerts"], report.get("groups", {})
    candidates = {}
    for key, members in groups.items():
        match = _candidate(rows, members, pods[key], metrics, scan)
        if match:
            candidates[key] = match
    times = {match[2]["timestamp"] for match in candidates.values()}
    if len(times) != 1:
        return False
    at = times.pop()
    payload = (fetch or _pending_vector)(url, at)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return False
    data = payload["data"]
    series = data.get("result")
    if (payload.get("status") != "success" or payload.get("warnings") or data.get("resultType") != "vector"
            or not isinstance(series, list) or len(series) > 1000):
        return False
    by_pod = defaultdict(list)
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("metric"), dict):
            return False
        labels = item["metric"]
        if not all(isinstance(v, str) for v in labels.values()):
            return False
        by_pod[(labels.get("namespace"), labels.get("pod"))].append(item)

    replacements, removed, new_metrics = [], set(), {}
    identity_rows = list(rows)
    for key, (prom, event, a, uid, condition, metric) in candidates.items():
        matches = by_pod[key]
        if len(matches) != 1:
            continue
        labels = matches[0]["metric"]
        if (matches[0].get("value") != [at, "1"] or labels.get("uid") != uid or labels.get("phase") != "Pending"
                or labels.get("job") != "kube-state-metrics" or labels.get("job") != a["labels"].get("job")
                or labels.get("cluster") != a["labels"].get("cluster") or labels.get("cluster_id", cluster) != cluster):
            continue
        # Bind a temporary identity for the partition guard. Stored source records stay byte-for-byte intact.
        pi, ei = prom["alert_indices"][0], event["alert_indices"][0]
        identity_rows[pi] = dict(a, raw=dict(a["raw"], entity_uid=uid))
        if target_identity(identity_rows[pi])["entity_uid"] != uid:
            continue
        group = make_group(identity_rows, [pi, ei], "verified_pod_scheduling")
        group["event_merge"] = {"basis": "uid_verified_unschedulable_pod", "causal_confirmed": False,
            "original_groups": [prom, event], "scheduling": {
                "pod_uid": uid, "scheduler": condition["scheduler"], "condition_since": condition["since"],
                "event_at": rows[ei]["timestamp"], "event_alert_id": rows[ei]["alert_id"], "phase_query": PENDING_QUERY,
                "phase_at": at, "phase_value": 1, "phase_labels": {k: labels[k] for k in
                    ("namespace", "pod", "uid", "phase", "job", "cluster", "cluster_id") if k in labels},
                "metric_evidence_id": metric["evidence_id"]}}
        new_metrics[group["group_id"]] = dict(metric, alert_ids=group["alert_ids"], measurement_alert_ids=prom["alert_ids"])
        removed.update((prom["group_id"], event["group_id"]))
        replacements.append(group)
    if not replacements:
        return False
    annotate_groups({"raw_alerts": rows, "groups": replacements})
    scan["groups"] = [g for g in scan["groups"] if g["group_id"] not in removed] + replacements
    for g in scan["groups"]:
        if g["frequency"]["status"] == "automatic_snapshot_baseline":
            g["frequency"] = {"status":"unknown", "observed_records":g["alert_count"],
                              "expected_records":None, "deviation_score":None}
            g["priority"]["frequency_score"] = 0
    scan["ranking"] = [g["group_id"] for g in sorted(scan["groups"], key=lambda g:
        (g["priority"]["severity_rank"], g["priority"]["frequency_score"], g["priority"]["rare"]), reverse=True)]
    report["groups"] = {k:v for k,v in metrics.items() if k not in removed} | new_metrics
    scan["candidate_pairs"], scan["relations"] = [], []
    scan["signature_version"] = 6
    scan["automatic_baseline"] = {"status": "warming_up", "reason": "pod scheduling grouping requires matching history"}
    scan["scheduling_compression"] = {"status": "applied", "merged_pairs": len(replacements)}
    regroup_stats(scan)
    return True
