"""Reviewed measurement queries for exact observed PromQL expressions.

This is a versioned allowlist, not a PromQL parser. A change to the full rule
expression requires review or an explicit agenticsre_metric_query annotation.
No whitespace normalization is used: quoted matcher contents are significant.
"""

REVIEWED_ADAPTERS = ({'adapter_id': 'TargetDown-v1',
  'expression': '100 * (count by (cluster, job, namespace, service) (up == 0) / count by (cluster, job, '
                'namespace, service) (up)) > 10',
  'query': '100 * (count by (cluster, job, namespace, service) (up == 0) / count by (cluster, job, '
           'namespace, service) (up))',
  'meaning': 'Percentage of scrape targets down, preserving the rule grouping.'},
 {'adapter_id': 'KubePodNotReady-v1',
  'expression': 'sum by (namespace, pod, job, cluster) (max by (namespace, pod, job, cluster) '
                '(kube_pod_status_phase{job="kube-state-metrics",namespace=~".*",phase=~"Pending|Unknown"}) '
                '* on (namespace, pod, cluster) group_left (owner_kind) topk by (namespace, pod, '
                'cluster) (1, max by (namespace, pod, owner_kind, cluster) '
                '(kube_pod_owner{owner_kind!="Job"}))) > 0',
  'query': 'sum by (namespace, pod, job, cluster) (max by (namespace, pod, job, cluster) '
           '(kube_pod_status_phase{job="kube-state-metrics",namespace=~".*",phase=~"Pending|Unknown"}) '
           '* on (namespace, pod, cluster) group_left (owner_kind) topk by (namespace, pod, cluster) '
           '(1, max by (namespace, pod, owner_kind, cluster) (kube_pod_owner{owner_kind!="Job"})))',
  'meaning': 'Pending/Unknown pod phase operand with the original non-Job owner join.'},
 {'adapter_id': 'etcdMembersDown-v1',
  'expression': 'max without (endpoint) (sum without (instance, pod) (up{job=~".*etcd.*"} == bool 0) or '
                'count without (To) (sum without (instance, pod) '
                '(rate(etcd_network_peer_sent_failures_total{job=~".*etcd.*"}[2m])) > 0.01)) > 0',
  'query': 'max without (endpoint) (sum without (instance, pod) (up{job=~".*etcd.*"} == bool 0) or '
           'count without (To) (sum without (instance, pod) '
           '(rate(etcd_network_peer_sent_failures_total{job=~".*etcd.*"}[2m])) > 0.01))',
  'meaning': 'Original rule operand combining unreachable members and peer-failure branches; not a pure '
             'member count.'},
 {'adapter_id': 'etcdInsufficientMembers-v1',
  'expression': 'sum without (instance, pod) (up{job=~".*etcd.*"} == bool 1) < ((count without '
                '(instance, pod) (up{job=~".*etcd.*"}) + 1) / 2)',
  'query': '(sum without (instance, pod) (up{job=~".*etcd.*"} == bool 1)) - (((count without (instance, '
           'pod) (up{job=~".*etcd.*"}) + 1) / 2))',
  'meaning': 'Reachable members minus the rule dynamic threshold; negative means insufficient. Original '
             'vector matching is preserved.'})


def reviewed_measurement(expression):
    """Return an independent descriptor only for an exact reviewed expression."""
    for adapter in REVIEWED_ADAPTERS:
        if expression == adapter['expression']:
            return dict(adapter)
    return None
