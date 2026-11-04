"""PromQL for pod metrics, including UID-bound pod-cgroup fallback.

Container-labelled metrics remain preferred. The fallback deliberately uses
systemd pod cgroups, a matching kube-state-metrics Pod UID, and duplicate-scrape
deduplication. Unsupported cgroup encodings remain missing rather than guessed.
"""
import json

_ID_PATTERN = r'.*pod([0-9a-f]{8})_([0-9a-f]{4})_([0-9a-f]{4})_([0-9a-f]{4})_([0-9a-f]{12})\.slice'
_POD_CGROUP = r'.*pod[^/]+\.slice'


def _labels(namespace='', pod_pattern='', extra=()):
    labels = ['pod!=""']
    if namespace:
        labels.append('namespace=' + json.dumps(namespace))
    if pod_pattern:
        labels.append('pod=~' + json.dumps(pod_pattern))
    labels.extend(extra)
    return ','.join(labels)


def _current_pods(namespace='', pod_pattern=''):
    labels = _labels(namespace, pod_pattern)
    return ('max by (namespace,pod,pod_uid) (label_replace(kube_pod_info{' + labels +
            '},"pod_uid","$1","uid","(.+)"))')


def _uid_cgroup(metric, namespace='', pod_pattern='', *, rate_window=''):
    labels = _labels(namespace, pod_pattern, ('container=""', 'id=~' + json.dumps(_POD_CGROUP)))
    if rate_window:
        labels += ',cpu="total"'
    series = metric + '{' + labels + '}'
    if rate_window:
        series = 'rate(' + series + '[' + rate_window + '])'
    groups = 'namespace,pod,id,cpu' if rate_window else 'namespace,pod,id'
    return ('label_replace(max by (' + groups + ') (' + series + '),"pod_uid",'
            '"$1-$2-$3-$4-$5","id",' + json.dumps(_ID_PATTERN) + ')')


def pod_cpu_cores(namespace='', pod_pattern=''):
    labels = _labels(namespace, pod_pattern, ('container!=""',))
    primary = ('sum by (namespace,pod) (max by (namespace,pod,container,cpu) '
               '(rate(container_cpu_usage_seconds_total{' + labels + '}[1m])))')
    fallback = ('sum by (namespace,pod) (' + _uid_cgroup('container_cpu_usage_seconds_total', namespace,
                pod_pattern, rate_window='5m') + ' * on (namespace,pod,pod_uid) group_left() '
                + _current_pods(namespace, pod_pattern) + ')')
    return '(' + primary + ') or on (namespace,pod) (' + fallback + ')'


def _uid_limit_ratio(metric, resource, namespace='', pod_pattern='', *, rate_window=''):
    common = _labels(namespace, pod_pattern)
    series = _uid_cgroup(metric, namespace, pod_pattern, rate_window=rate_window)
    current = _current_pods(namespace, pod_pattern)
    numerator = ('sum by (namespace,pod,pod_uid) (' + series +
                 ' * on (namespace,pod,pod_uid) group_left() ' + current + ')')
    limit = ('sum by (namespace,pod,pod_uid) (label_replace(max by (namespace,pod,container,uid) '
             '(kube_pod_container_resource_limits{' + common + ',resource=' + json.dumps(resource) + '}),'
             '"pod_uid","$1","uid","(.+)"))')
    return ('sum by (namespace,pod) (' + numerator + ' / on (namespace,pod,pod_uid) (' + limit + ' > 0))')


def pod_cpu_limit_ratio(namespace='', pod_pattern=''):
    common = _labels(namespace, pod_pattern)
    labelled = _labels(namespace, pod_pattern, ('container!=""',))
    cpu = ('sum by (namespace,pod) (max by (namespace,pod,container,cpu) '
           '(rate(container_cpu_usage_seconds_total{' + labelled + '}[1m])))')
    limit = ('sum by (namespace,pod) (max by (namespace,pod,container) '
             '(kube_pod_container_resource_limits{' + common + ',resource="cpu"}))')
    primary = cpu + ' / on (namespace,pod) (' + limit + ' > 0)'
    fallback = _uid_limit_ratio('container_cpu_usage_seconds_total', 'cpu', namespace, pod_pattern, rate_window='5m')
    return '(' + primary + ') or on (namespace,pod) (' + fallback + ')'


def pod_memory_limit_ratio(namespace='', pod_pattern=''):
    common = _labels(namespace, pod_pattern)
    labelled = _labels(namespace, pod_pattern, ('container!=""',))
    primary_numerator = ('sum by (namespace,pod) (max by (namespace,pod,container) '
                         '(container_memory_working_set_bytes{' + labelled + '}))')
    primary_limit = ('sum by (namespace,pod) (max by (namespace,pod,container) '
                     '(kube_pod_container_resource_limits{' + common + ',resource="memory"}))')
    primary = primary_numerator + ' / on (namespace,pod) (' + primary_limit + ' > 0)'
    fallback = _uid_limit_ratio('container_memory_working_set_bytes', 'memory', namespace, pod_pattern)
    return '(' + primary + ') or on (namespace,pod) (' + fallback + ')'
