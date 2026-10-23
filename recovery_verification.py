"""Opt-in, operator-configured HTTP recovery probes; never accept request URLs."""
import math
import time
from urllib.parse import urlsplit

import requests


def verify_business_recovery(infrastructure, *, profiles, cluster_id, namespace, service):
    result = dict(infrastructure)
    result["verification_scope"] = "infrastructure"
    if not profiles:
        return result
    eligible = (result.get("status") == "verified" and result.get("recovered") is True) or (
        result.get("reason") == "business_probe_required" and result.get("action_completed") is True)
    if not eligible:
        return result
    # Exact scope matching; no global/default-cluster fallback for another cluster.
    matches = [p for p in profiles if isinstance(p, dict) and service and
               p.get("service") == service and p.get("namespace") == namespace and
               p.get("cluster_id", "") == (cluster_id or "")]
    if not matches:
        return {**result, "status": "unknown", "recovered": None,
                "reason": "business_probe_not_configured_for_target"}
    if len(matches) != 1:
        return {**result, "status": "unknown", "recovered": None,
                "reason": "ambiguous_business_probe"}
    profile = matches[0]
    try:
        url = profile["url"]
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Invalid URL")
        samples = profile.get("samples", 3)
        interval = profile.get("interval_seconds", 2)
        timeout = profile.get("timeout_seconds", 3)
        limit = profile.get("max_header_latency_ms", 1000)
        expected = profile.get("expected_status", 200)
        if type(samples) is not int or not 2 <= samples <= 5:
            raise ValueError("Invalid sample count")
        if type(expected) is not int or not 200 <= expected <= 299:
            raise ValueError("Invalid expected status")
        for value, low, high in ((interval, 1, 10), (timeout, .1, 5), (limit, 1, 10000)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid timing")
    except (KeyError, TypeError, ValueError):
        return {**result, "status": "unknown", "recovered": None,
                "reason": "invalid_business_probe_config"}

    observations = []
    started = time.monotonic()
    outcome = True
    reason = "business_probe_passed"
    for index in range(samples):
        if index:
            time.sleep(interval)
        before = time.monotonic()
        try:
            # No redirects, ambient proxy or credentials; do not retain response bodies.
            with requests.Session() as session:
                session.trust_env = False
                with session.get(url, timeout=timeout, allow_redirects=False, stream=True) as response:
                    elapsed_ms = (time.monotonic() - before) * 1000
                    passed = response.status_code == expected and elapsed_ms <= limit
                    observations.append({"sample": index + 1, "at": time.time(),
                        "status_code": response.status_code, "header_latency_ms": round(elapsed_ms, 3),
                        "passed": passed})
            if not passed:
                outcome, reason = False, "business_probe_failed"
                break
        except requests.RequestException:
            observations.append({"sample": index + 1, "at": time.time(),
                                 "passed": None, "error": "probe_unavailable"})
            outcome, reason = None, "business_probe_unavailable"
            break
    probe = {"type": "business_http", "cluster_id": cluster_id or "", "namespace": namespace,
             "service": service, "required_samples": samples, "interval_seconds": interval,
             "expected_status": expected, "max_header_latency_ms": limit,
             "elapsed_seconds": round(time.monotonic() - started, 3), "observations": observations,
             "recovered": outcome}
    return {**result, "infrastructure_recovered": result.get("recovered"),
            "verification_scope": "infrastructure_and_business_http",
            "status": "unknown" if outcome is None else "verified",
            "recovered": outcome, "reason": reason,
            "checks": [*(result.get("checks") or []), probe]}
