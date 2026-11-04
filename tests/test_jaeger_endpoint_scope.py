"""A configured trace backend must not be replaced by another tenant's backend."""
import json
from types import SimpleNamespace

from tools.observability import JaegerTool


def test_healthy_explicit_backend_is_not_replaced_by_other_app(monkeypatch):
    tool = JaegerTool('http://hotel.example:16686')
    def services(url):
        return ['frontend', 'search'] if 'hotel.example' in url else ['compose-post-service']
    monkeypatch.setattr(tool, '_services_for', services)
    def cluster(command, **kwargs):
        value = {'items': [{'metadata': {'namespace': 'social', 'name': 'trace-store'},
                            'spec': {'clusterIP': '10.0.0.2', 'ports': [{'port': 16686}]}}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value if 'get svc' in command else {'items': []}))
    monkeypatch.setattr('tools.observability.subprocess.run', cluster)
    assert tool._discover_base_url() == 'http://hotel.example:16686'


def test_unreachable_explicit_backend_does_not_fall_back_to_other_scope(monkeypatch):
    tool = JaegerTool('http://unavailable.example:16686')
    monkeypatch.setattr(tool, '_services_for', lambda url: None if 'unavailable.example' in url else ['compose-post-service'])
    def cluster(command, **kwargs):
        value = {'items': [{'metadata': {'namespace': 'social', 'name': 'trace-store'},
                            'spec': {'clusterIP': '10.0.0.2', 'ports': [{'port': 16686}]}}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value if 'get svc' in command else {'items': []}))
    monkeypatch.setattr('tools.observability.subprocess.run', cluster)
    assert tool._discover_base_url() == 'http://unavailable.example:16686'
    def unavailable(url, **kwargs):
        assert url.startswith('http://unavailable.example:16686/')
        raise TimeoutError('backend unavailable')
    monkeypatch.setattr(tool.session, 'get', unavailable)
    result = tool.execute(service='search')
    assert not result.success
