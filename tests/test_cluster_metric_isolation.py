from types import SimpleNamespace
import web_app.app as dashboard


def test_unreachable_cluster_metrics_never_fall_back_to_global(monkeypatch):
    monkeypatch.setattr(dashboard, '_prometheus_api_ok', lambda url: url == 'http://other-cluster')
    monkeypatch.setattr(dashboard, '_discover_prometheus_url', lambda: 'http://other-cluster')
    monkeypatch.setattr(dashboard, '_get_config', lambda: SimpleNamespace(
        observability=SimpleNamespace(prometheus_url='http://other-cluster')))
    cluster = {'id':'ai', 'prometheus_url':'http://ai-unavailable'}
    assert dashboard._reachable_cluster_prometheus_url(cluster) == 'http://ai-unavailable'
    assert dashboard._reachable_cluster_prometheus_url({'id':'unconfigured'}) == ''
    assert dashboard._reachable_cluster_prometheus_url(cluster, 'http://explicit') == 'http://explicit'
    assert dashboard._reachable_cluster_prometheus_url(None) == 'http://other-cluster'
