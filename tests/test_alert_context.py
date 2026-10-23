import copy
import json
from agents.alert_compression import compress_alerts
from agents.alert_history import AlertHistory
from agents.alert_context import trace_evidence, rollout_evidence, qualify_context, recall_context, scoped_trace_routes


def scan(count=1, cluster="c1"):
    result = compress_alerts([dict(name="error", message="failed", source="logs", cluster_id=cluster,
        namespace="shop", pod="api", timestamp=1000+i) for i in range(count)])
    result.update(origin="live_scan", cluster_id=cluster, collection={"kubectl":"ok", "prometheus":"ok"}, scan_id=f"scan-{count}")
    return result


def test_history_complete_distinct_buckets_scope_and_current_exclusion(tmp_path):
    h = AlertHistory(tmp_path/'history.sqlite3')
    for t in [1000, 1001, 1002]:
        h.record(scan(), "shop", now=t)
    assert h.apply(scan(10), "shop", now=1500)["automatic_baseline"]["snapshots"] == 1
    h.record(scan(), "shop", now=1300)
    h.record(scan(), "shop", now=1600)
    current = h.apply(scan(10), "shop", now=1900)
    assert current["automatic_baseline"]["status"] == "ready"
    assert current["groups"][0]["frequency"]["expected_records"] == 1
    assert current["groups"][0]["frequency"]["deviation_score"] == 9
    assert h.apply(scan(), "other", now=1900)["automatic_baseline"]["status"] == "warming_up"
    assert h.apply(scan(cluster="c2"), "shop", now=1900)["automatic_baseline"]["snapshots"] == 0
    assert h.apply(scan(), "shop", now=100000)["automatic_baseline"]["snapshots"] == 0


def test_imports_and_failed_collection_do_not_contaminate_history(tmp_path):
    h = AlertHistory(tmp_path/'history.sqlite3')
    for t, change in [(1000, {"origin":"import"}), (1300, {"degraded":True}), (1600, {"collection":{"kubectl":"timeout"}})]:
        record = scan(); record.update(change); h.record(record, "shop", now=t)
    assert h.apply(scan(), "shop", now=1900)["automatic_baseline"]["snapshots"] == 0


def test_bootstrap_uses_stored_scope_and_original_time(tmp_path):
    h = AlertHistory(tmp_path/'history.sqlite3')
    for index,t in enumerate([1000,1300,1600]):
        record=scan();record.update(scan_scope="shop", created_at=t)
        (tmp_path/f'scan-{index}.json').write_text(json.dumps(record))
    h.bootstrap(tmp_path,"c1","shop",now=1900)
    assert h.apply(scan(),"shop",now=1900)["automatic_baseline"]["status"] == "ready"
    assert h.apply(scan(),"",now=1900)["automatic_baseline"]["snapshots"] == 0
    assert h.apply(scan(cluster="c2"),"shop",now=1900)["automatic_baseline"]["snapshots"] == 0


def inventory():
    return {"pods": [{"namespace":"shop", "name":name, "uid":uid, "owner_kind":"ReplicaSet", "owner_uid":"rs2"}
                     for name, uid in [("api","p1"),("db","p2")]]}


def trace():
    return {"data":[{"traceID":"trace1", "processes":{
        "a":{"serviceName":"api", "tags":[{"key":"k8s.pod.uid","value":"p1"}]},
        "b":{"serviceName":"db", "tags":[{"key":"k8s.pod.uid","value":"p2"}]}},
        "spans":[{"spanID":"a", "processID":"a", "startTime":1000000000, "duration":2000000},
                 {"spanID":"b", "processID":"b", "startTime":1001000000, "duration":100,
                  "references":[{"refType":"CHILD_OF", "traceID":"trace1", "spanID":"a"}]}]}]}


def test_trace_requires_exact_identity_same_trace_and_valid_parent_time():
    payload = trace()
    result = trace_evidence(payload, "c1", inventory(), "fixture")
    assert len(result["edges"]) == 1
    assert result["edges"][0]["source"] == "c1|shop|pod|api"
    duplicate = trace(); duplicate["data"].append(copy.deepcopy(duplicate["data"][0]))
    assert len(trace_evidence(duplicate, "c1", inventory(), "fixture")["edges"]) == 1
    foreign = trace(); foreign["data"][0]["processes"]["b"]["tags"].append({"key":"k8s.cluster.name", "value":"c2"})
    assert not trace_evidence(foreign, "c1", inventory(), "fixture")["edges"]
    payload["data"][0]["processes"]["b"]["tags"] = []
    assert not trace_evidence(payload, "c1", inventory(), "fixture")["edges"]
    payload = trace(); payload["data"][0]["spans"][1]["references"][0]["traceID"] = "other"
    assert not trace_evidence(payload, "c1", inventory(), "fixture")["edges"]
    payload = trace(); payload["data"][0]["spans"][1]["startTime"] = 999
    assert not trace_evidence(payload, "c1", inventory(), "fixture")["edges"]


def replicas():
    return {"items":[{"metadata":{"namespace":"shop", "uid":f"rs{i}", "name":f"api-{i}",
        "creationTimestamp":f"2026-09-15T00:0{i}:00Z", "annotations":{"deployment.kubernetes.io/revision":str(i)},
        "ownerReferences":[{"kind":"Deployment", "controller":True, "uid":"deployment1", "name":"api"}]},
        "spec":{"template":{"spec":{"containers":[{"name":"api","image":f"app:v{i}","env":[{"name":"PASSWORD","value":"private-value"}]}]}}}}
        for i in [1,2]]}


def test_rollout_requires_contiguous_revisions_uid_mapping_and_no_rollback_reuse():
    result = rollout_evidence(replicas(), "c1", inventory(), "fixture")
    assert len(result["changes"]) == 1
    assert result["changes"][0]["targets"] == ["c1|shop|pod|api", "c1|shop|pod|db"]
    assert "private-value" not in json.dumps(result)
    payload=replicas(); payload["items"][1]["metadata"]["annotations"]["deployment.kubernetes.io/revision-history"]="1"
    assert not rollout_evidence(payload,"c1",inventory(),"fixture")["changes"]
    payload=replicas(); payload["items"][1]["metadata"]["annotations"]["deployment.kubernetes.io/revision"]="4"
    assert not rollout_evidence(payload,"c1",inventory(),"fixture")["changes"]


def test_observed_call_recalls_dissimilar_alerts_and_expired_context_does_not():
    result=compress_alerts([dict(name="a",message="CPU high",cluster_id="c1",namespace="shop",pod="api",timestamp=1000),
                            dict(name="b",message="disk full",cluster_id="c1",namespace="shop",pod="db",timestamp=1002)])
    result["context_evidence"]={"traces":trace_evidence(trace(),"c1",inventory(),"fixture")}
    recall_context(result)
    assert result["context_candidate_pairs"] == 1
    a,b=result["groups"]
    assert qualify_context(result,a,b,"observed_call")["status"] == "pass"
    assert qualify_context(result,a,b,"propagates_to")["status"] == "unknown"
    assert qualify_context(result,b,a,"propagates_to")["status"] == "unknown"
    result["context_evidence"]["traces"]["edges"][0]["at"] = 100
    result["candidate_pairs"] = []
    recall_context(result)
    assert result["context_candidate_pairs"] == 0


def test_fallback_routes_never_use_another_namespace_or_ambiguous_backend():
    svc = {"name":"jaeger", "namespace":"shop", "cluster_ip":"10.1.2.3", "ports":["16686"]}
    inv = {"services":[svc, dict(svc, namespace="other", cluster_ip="10.2.3.4")]}
    assert scoped_trace_routes(scan(), inv) == [{"namespace":"shop", "url":"http://10.1.2.3:16686"}]
    inv["services"].append(dict(svc, cluster_ip="10.3.4.5"))
    assert scoped_trace_routes(scan(), inv) == []


def test_namespace_proxy_supplies_trace_data_without_mutating_observations():
    import asyncio
    from agents.alert_context import collect_scoped_traces
    result = scan()
    inv = inventory()
    inv["pods"][0]["labels"] = {"app":"api"}
    inv["services"] = [{"name":"api", "namespace":"shop", "selector":{"app":"api"}},
        {"name":"jaeger", "namespace":"shop", "cluster_ip":"10.1.2.3", "ports":["16686"]}]
    calls = []
    def proxy(namespace, path, params):
        calls.append((namespace,path))
        return {"data":["api"]} if path == '/api/services' else trace()
    before = copy.deepcopy(result)
    data = asyncio.run(collect_scoped_traces('',result,inv,proxy_fetch=proxy))
    assert data["status"] == "mapped"
    assert data["routing"] == "namespace_service_proxy"
    assert len(data["edges"]) == 1
    assert calls == [('shop','/api/services'),('shop','/api/traces')]
    assert result == before
