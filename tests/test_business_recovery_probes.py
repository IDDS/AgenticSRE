"""Real loopback HTTP observations, plus fail-closed scope/config cases."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from recovery_verification import verify_business_recovery

INFRA = {"status": "verified", "recovered": True, "checks": []}


@pytest.fixture
def endpoint():
    counts = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            if self.path == '/slow':
                time.sleep(.04)
            code = 503 if self.path == '/fail' or (self.path == '/flaky' and counts[self.path] > 1) else 200
            if self.path == '/redirect':
                code = 302
            self.send_response(code)
            if code == 302:
                self.send_header('Location', '/healthy')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', counts
    server.shutdown()
    server.server_close()
    thread.join()


def profile(base_url, **overrides):
    return {'url': base_url, 'cluster_id': 'test', 'namespace': 'test', 'service': 'api',
            'samples': 2, 'interval_seconds': 1, **overrides}


def run(profiles, infrastructure=None, **scope):
    return verify_business_recovery(INFRA if infrastructure is None else infrastructure,
        profiles=profiles, **({'cluster_id': 'test', 'namespace': 'test', 'service': 'api'} | scope))


def test_real_http_continuous_success(endpoint):
    url, counts = endpoint
    result = run([profile(url + '/healthy')])
    assert result['recovered'] is True
    check = result['checks'][-1]
    assert len(check['observations']) == 2
    assert check['elapsed_seconds'] >= 1
    assert counts['/healthy'] == 2
    assert result['verification_scope'] == 'infrastructure_and_business_http'


@pytest.mark.parametrize('path', ['/fail', '/flaky', '/redirect', '/slow'])
def test_real_http_failure_is_not_recovery(endpoint, path):
    url, counts = endpoint
    kwargs = {'max_header_latency_ms': 1} if path == '/slow' else {}
    result = run([profile(url + path, **kwargs)])
    assert result['recovered'] is False
    assert result['reason'] == 'business_probe_failed'
    assert '/healthy' not in counts  # redirect was not followed


@pytest.mark.parametrize('override', [{'cluster_id': 'other'}, {'namespace': 'other'}, {'service': 'other'}])
def test_wrong_scope_never_queries(endpoint, override):
    url, counts = endpoint
    result = run([profile(url + '/healthy')], **override)
    assert result['recovered'] is None
    assert result['reason'] == 'business_probe_not_configured_for_target'
    assert not counts


@pytest.mark.parametrize('change', [{'samples': 1}, {'samples': True}, {'interval_seconds': 0},
    {'timeout_seconds': 999}, {'max_header_latency_ms': float('nan')},
    {'expected_status': 503}, {'url': 'file:///tmp/test'}, {'url': 'http://user:pass@localhost'}])
def test_invalid_operator_config_fails_closed(endpoint, change):
    url, counts = endpoint
    assert run([profile(url, **change)])['reason'] == 'invalid_business_probe_config'
    assert not counts


def test_ambiguous_config_fails_closed(endpoint):
    url, counts = endpoint
    p = profile(url)
    assert run([p, p])['reason'] == 'ambiguous_business_probe'
    assert not counts


def test_failed_infrastructure_never_promoted(endpoint):
    url, counts = endpoint
    assert run([profile(url)], {'status': 'verified', 'recovered': False})['recovered'] is False
    assert not counts


def test_no_config_preserves_infrastructure_scope():
    result = run([])
    assert result['recovered'] is True
    assert result['verification_scope'] == 'infrastructure'


def test_network_cleanup_requires_and_uses_real_probe(endpoint):
    url, _ = endpoint
    result = run([profile(url + '/healthy')], {'status': 'unknown', 'recovered': None,
        'reason': 'business_probe_required', 'action_completed': True})
    assert result['recovered'] is True
    assert result['infrastructure_recovered'] is None


def test_unavailable_probe_is_unknown():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        # Bound but not listening: actual refused connection, no fabricated response.
        result = run([profile(f'http://127.0.0.1:{port}', timeout_seconds=.1)])
    assert result['recovered'] is None
    assert result['reason'] == 'business_probe_unavailable'
