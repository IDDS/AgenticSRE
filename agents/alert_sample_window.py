"""Measure grouping on stored firing samples in one closed, cluster-scoped query."""
from collections import Counter
import json
import shlex
from urllib.parse import quote, urlencode

import requests

from agents.alert_agent import Alert
from agents.alert_compression import compress_alerts


WINDOW = 300
LIMIT = 10000


class WindowUnavailable(ValueError):
    """A user-visible reason that a complete rate cannot be computed."""


def read_matrix(url, shell, end):
    params = {"query": 'ALERTS{alertstate="firing"}[5m]', "time": end, "timeout": "8s", "limit": LIMIT + 1}
    if url:
        response = requests.get(url.rstrip("/") + "/api/v1/query", params=params, timeout=12)
        response.raise_for_status()
        return response.json()

    # The service proxy follows the selected kubeconfig/context, including jump hosts.
    services = shell("kubectl get services -A --field-selector metadata.name=prometheus-operated -o json", 10)
    if not services.get("ok"):
        raise WindowUnavailable("无法读取所选集群的 Prometheus 服务")
    endpoints = []
    for service in json.loads(services["stdout"]).get("items", []):
        meta = service["metadata"]
        if meta["name"] == "prometheus-operated" and meta.get("namespace"):
            for port in service["spec"].get("ports", []):
                if port.get("port") == 9090 and port.get("name"):
                    endpoints.append((meta["namespace"], port["name"]))
    if len(endpoints) != 1:
        raise WindowUnavailable("请为所选集群配置唯一的 Prometheus 地址")
    namespace, port = endpoints[0]
    path = (f"/api/v1/namespaces/{quote(namespace, safe='')}/services/prometheus-operated:{quote(port, safe='')}"
            "/proxy/api/v1/query?" + urlencode(params))
    response = shell(f"kubectl get --raw {shlex.quote(path)} --request-timeout=12s", 15)
    if not response.get("ok"):
        raise WindowUnavailable("所选集群的 Prometheus 查询失败")
    return json.loads(response["stdout"])


def summarize_window(payload, cluster, end):
    data = payload.get("data") if isinstance(payload, dict) else None
    if (not isinstance(data, dict) or payload.get("status") != "success" or payload.get("warnings")
            or data.get("resultType") != "matrix" or not isinstance(data.get("result"), list)):
        raise WindowUnavailable("Prometheus 未返回完整的告警时间序列")
    records, series_seen = [], set()
    for series in data["result"]:
        if not isinstance(series, dict) or not isinstance(series.get("values"), list) or not series["values"] or "histograms" in series:
            raise WindowUnavailable("告警序列缺少有效样本")
        if len(records) + len(series["values"]) > LIMIT:
            raise WindowUnavailable("五分钟内超过 10000 个样本，本次未计算压缩率")
        metric = series.get("metric")
        if not isinstance(metric, dict) or metric.get("__name__") != "ALERTS":
            raise WindowUnavailable("告警序列标签不完整")
        key = json.dumps(metric, sort_keys=True)
        if key in series_seen:
            raise WindowUnavailable("Prometheus 返回了重复序列")
        series_seen.add(key)
        previous = end - WINDOW
        for pair in series["values"]:
            try:
                alert = Alert.from_prometheus_sample({"metric": metric, "value": pair}, "", cluster)
            except (TypeError, ValueError, OverflowError) as exc:
                raise WindowUnavailable("告警样本无效") from exc
            if not previous < alert.timestamp <= end:
                raise WindowUnavailable("告警样本时间越界、重复或乱序")
            previous = alert.timestamp
            records.append(vars(alert))
    result = compress_alerts(records, cluster_id=cluster, time_window=WINDOW)
    samples = Counter(r["name"] for r in result["raw_alerts"])
    groups = Counter(g["representative"] for g in result["groups"])
    return {"cluster_id": cluster, "start": end - WINDOW, "end": end, "series": len(series_seen),
            "samples": len(records), "groups": len(result["groups"]),
            "compression_ratio": result["compression_ratio"] if records else None,
            "rules": [{"name": name, "samples": n, "groups": groups[name]}
                      for name, n in samples.most_common()]}
