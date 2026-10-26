"""Business recovery requires the configured response contract, not just HTTP 200."""
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from recovery_verification import verify_business_recovery


@pytest.fixture
def business_endpoint():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get('Accept-Encoding')))
            code, headers = 200, {}
            payload = {'status': 1, 'data': [{'available': True}], 'private': 'fixture-not-for-receipts'}
            if self.path == '/wrong-status':
                payload['status'] = 0
            elif self.path == '/boolean':
                payload['status'] = True
            elif self.path == '/empty':
                payload['data'] = []
            elif self.path == '/missing':
                payload.pop('status')
            elif self.path == '/null':
                payload['status'] = None
            elif self.path == '/flaky' and sum(p == '/flaky' for p, _ in calls) > 1:
                payload['status'] = 0
            elif self.path == '/status-failed':
                code = 503
            elif self.path == '/redirect':
                code, headers['Location'] = 302, '/ok'
            body = json.dumps(payload).encode()
            if self.path.startswith('/decimal/'):
                body = b'{"status":' + self.path[len('/decimal/'):].encode() + b',"data":[1]}'
            elif self.path == '/invalid':
                body = b'<html>upstream error</html>'
            elif self.path == '/duplicate':
                body = b'{"status":0,"status":1,"data":[1]}'
            elif self.path == '/nonfinite':
                body = b'{"status":1,"data":[NaN]}'
            elif self.path == '/nested':
                body = b'{"status":1,"data":' + b'[' * 40 + b'0' + b']' * 40 + b'}'
            elif self.path == '/large':
                body = b' ' * 65537
            elif self.path == '/encoded':
                headers['Content-Encoding'] = 'gzip'
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            if self.path in ('/chunked', '/chunked-slow'):
                self.send_header('Transfer-Encoding', 'chunked')
            elif self.path == '/broken-body':
                self.send_header('Content-Length', str(len(body) + 100))
                self.send_header('Connection', 'close')
                self.close_connection = True
            elif self.path != '/large-unannounced':
                self.send_header('Content-Length', str(len(body)))
            else:
                body = b' ' * 65537
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                if self.path in ('/chunked', '/chunked-slow'):
                    for index in range(0, len(body), 3):
                        part = body[index:index + 3]
                        self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
                        self.wfile.flush()
                        if self.path == '/chunked-slow':
                            time.sleep(.03)
                    self.wfile.write(b'0\r\n\r\n')
                elif self.path == '/slow-body':
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.03)
                else:
                    if self.path == '/body-stall':
                        time.sleep(.3)
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', calls
    server.shutdown()
    server.server_close()
    worker.join()


def profile(url, **changes):
    return {'url': url, 'cluster_id': 'test', 'namespace': 'test', 'service': 'api',
            'samples': 2, 'interval_seconds': 1,
            'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': 1},
                                {'path': ['data'], 'op': 'non_empty'}], **changes}


def verify(p):
    return verify_business_recovery({'status': 'verified', 'recovered': True, 'checks': []},
        profiles=[p], cluster_id='test', namespace='test', service='api')


def test_real_json_contract_passes_all_samples_without_value_disclosure(business_endpoint):
    url, calls = business_endpoint
    p = profile(url + '/ok')
    before = copy.deepcopy(p)
    result = verify(p)
    assert result['recovered'] is True
    assert result['business_validation'] == 'json_assertions'
    check = result['checks'][-1]
    assert check['validation_level'] == 'json_assertions'
    assert len(check['observations']) == 2
    assert all(o['assertions_passed'] is True and o['body_bytes'] > 0 for o in check['observations'])
    assert calls == [('/ok', 'identity'), ('/ok', 'identity')]
    serialized = json.dumps(result)
    assert 'fixture-not-for-receipts' not in serialized and url not in serialized
    assert 'actual_value' not in serialized and 'json_assertions' not in check
    assert p == before


@pytest.mark.parametrize('path', ['/wrong-status', '/boolean', '/empty', '/missing', '/invalid', '/duplicate', '/nonfinite', '/flaky'])
def test_http_200_with_failed_contract_cannot_be_recovery(business_endpoint, path):
    url, calls = business_endpoint
    result = verify(profile(url + path))
    assert result['recovered'] is False
    assert result['reason'] == 'business_probe_failed'
    observations = result['checks'][-1]['observations']
    assert observations[-1]['passed'] is False
    assert len(observations) == (2 if path == '/flaky' else 1)


def test_missing_json_field_is_different_from_explicit_null(business_endpoint):
    url, _ = business_endpoint
    assertions = [{'path': ['status'], 'op': 'equals', 'value': None}]
    assert verify(profile(url + '/null', json_assertions=assertions))['recovered'] is True
    assert verify(profile(url + '/missing', json_assertions=assertions))['recovered'] is False


def test_array_path_and_numeric_json_equality(business_endpoint):
    url, _ = business_endpoint
    assertions = [{'path': ['data', 0, 'available'], 'op': 'equals', 'value': True},
                  {'path': ['status'], 'op': 'equals', 'value': 1.0}]
    assert verify(profile(url + '/ok', json_assertions=assertions))['recovered'] is True


@pytest.mark.parametrize('literal,expected,outcome', [
    ('9007199254740993.0', 9007199254740992, False),
    ('9007199254740992.0', 9007199254740992, True),
    ('1.0', 1, True),
    ('0.10000000000000001', 0.1, False),
])
def test_json_number_comparison_does_not_round_distinct_values(business_endpoint, literal, expected, outcome):
    url, _ = business_endpoint
    result = verify(profile(url + '/decimal/' + literal,
        json_assertions=[{'path': ['status'], 'op': 'equals', 'value': expected}]))
    assert result['recovered'] is outcome


@pytest.mark.parametrize('path', ['/large', '/large-unannounced', '/nested', '/encoded'])
def test_unverifiable_body_is_unknown_not_healthy(business_endpoint, path):
    url, _ = business_endpoint
    result = verify(profile(url + path))
    assert result['recovered'] is None
    assert result['status'] == 'unknown'
    assert result['checks'][-1]['observations'][-1]['passed'] is None


def test_fast_headers_cannot_hide_slow_business_body(business_endpoint):
    url, _ = business_endpoint
    started = time.monotonic()
    result = verify(profile(url + '/slow-body', max_response_latency_ms=100, timeout_seconds=.1))
    assert result['recovered'] is False
    assert result['checks'][-1]['observations'][0]['error'] == 'response_latency_exceeded'
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize('path', ['/body-stall', '/broken-body'])
def test_unavailable_body_is_unknown(business_endpoint, path):
    url, _ = business_endpoint
    result = verify(profile(url + path, timeout_seconds=.1))
    assert result['recovered'] is None
    assert result['reason'] == 'business_probe_unavailable'


def test_chunked_json_is_checked_and_slow_chunked_body_is_bounded(business_endpoint):
    url, _ = business_endpoint
    assert verify(profile(url + '/chunked'))['recovered'] is True
    started = time.monotonic()
    result = verify(profile(url + '/chunked-slow', timeout_seconds=.1, max_response_latency_ms=100))
    assert result['recovered'] is False
    assert result['checks'][-1]['observations'][0]['error'] == 'response_latency_exceeded'
    assert time.monotonic() - started < 1.5


def test_config_edit_during_window_does_not_change_assertion_contract(business_endpoint, monkeypatch):
    import recovery_verification as probes
    url, _ = business_endpoint
    p = profile(url + '/ok')
    sleep = time.sleep
    def edit_during_interval(seconds):
        p['json_assertions'][0]['value'] = 0
        sleep(seconds)
    monkeypatch.setattr(probes.time, 'sleep', edit_during_interval)
    assert verify(p)['recovered'] is True
    assert p['json_assertions'][0]['value'] == 0


@pytest.mark.parametrize('change', [
    {'json_assertions': []}, {'json_assertions': None}, {'json_assertions': [{'path': ['status'], 'op': 'eval', 'value': 1}]},
    {'json_assertions': [{'path': [True], 'op': 'equals', 'value': 1}]},
    {'json_assertions': [{'path': [-1], 'op': 'equals', 'value': 1}]},
    {'json_assertions': [{'path': ['status'], 'op': 'equals'}]},
    {'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': float('nan')}]},
    {'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': {'unsafe': 'shape'}}]},
    {'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': 1, 'typo': True}]},
    {'json_assertions': [{'path': ['status'], 'op': 'non_empty', 'value': True}]},
    {'json_assertions': [{'path': ['data'], 'op': 'non_empty'}] * 17},
    {'max_body_bytes': 65537}, {'max_body_bytes': True}, {'max_response_latency_ms': float('inf')},
    {'json_assertion': []},
])
def test_invalid_semantic_config_never_queries(business_endpoint, change):
    url, calls = business_endpoint
    result = verify(profile(url + '/ok', **change))
    assert result['recovered'] is None
    assert result['reason'] == 'invalid_business_probe_config'
    assert not calls


def test_header_only_mode_explicitly_has_no_semantic_claim(business_endpoint):
    url, _ = business_endpoint
    p = profile(url + '/wrong-status')
    p.pop('json_assertions')
    result = verify(p)
    assert result['recovered'] is True  # Existing opt-in semantics are preserved.
    assert result['business_validation'] == 'http_headers'


@pytest.mark.parametrize('path', ['/status-failed', '/redirect'])
def test_bad_http_does_not_become_json_success(business_endpoint, path):
    url, calls = business_endpoint
    assert verify(profile(url + path))['recovered'] is False
    assert all(p != '/ok' for p, _ in calls)


@pytest.mark.parametrize('path,expected', [('/wrong-status', False), ('/ok', True), ('/large', None)])
def test_existing_reverification_api_uses_semantics_without_replaying_action(business_endpoint, monkeypatch, tmp_path, path, expected):
    import web_app.app as web
    url, _ = business_endpoint
    monkeypatch.setattr(web, '_HEAL_RUNS_FILE', tmp_path / 'runs.json')
    monkeypatch.setattr(web, '_HEAL_ARTIFACT_DIR', tmp_path / 'runs')
    monkeypatch.setattr(web, '_get_config', lambda: SimpleNamespace(remediation=SimpleNamespace(
        business_recovery_probes=[profile(url + path)])))
    monkeypatch.setattr(web, '_verify_heal_recovery', lambda **kw: {'status': 'verified', 'recovered': True})
    monkeypatch.setattr(web, '_shell_sync', lambda *a, **kw: pytest.fail('no action replay'))
    monkeypatch.setattr(web, '_get_fault_store', lambda: pytest.fail('no causal credit for a later observation'))
    original = {'id': 'heal-json', 'status': 'verification_pending', 'dry_run': False,
                'action_success': True, 'success': None, 'cluster_id': 'test', 'namespace': 'test',
                'verification_target': {'service': 'api'}, 'commands': ['saved-action'],
                'results': [{'ok': True}], 'finished_at': 123,
                'verification': {'status': 'unknown', 'recovered': None}}
    web._save_heal_run(original)
    client = TestClient(web.app)
    response = client.post('/api/heal/runs/heal-json/verify')
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['success'] is expected
    assert saved['status'] == ('verification_pending' if expected is None else 'succeeded' if expected else 'failed')
    assert saved['commands'] == original['commands'] and saved['finished_at'] == 123
    assert saved['recovery_history_record']['recorded'] is False
    assert client.get('/api/heal/runs/heal-json').json() == saved
    assert 'fixture-not-for-receipts' not in json.dumps(saved)


def test_recovery_implementation_is_included_in_application_image_copy():
    import inspect
    from pathlib import Path
    import shlex
    import web_app.app as web
    root = Path(web.__file__).resolve().parents[1]
    relative = Path(inspect.getfile(verify_business_recovery)).resolve().relative_to(root).as_posix()
    sources = [source for line in (root / 'Dockerfile').read_text().splitlines()
               if line.startswith('COPY ') for source in shlex.split(line)[1:-1]]
    assert any(relative == source or source.endswith('/') and relative.startswith(source) for source in sources), (
        'The actual recovery implementation is absent from application image COPY sources')


@pytest.mark.parametrize('profiles', [None, {}, 'invalid'])
@pytest.mark.parametrize('infrastructure_recovered', [False, True])
def test_invalid_profile_collection_preserves_known_infrastructure_failure(profiles, infrastructure_recovered):
    result = verify_business_recovery({'status': 'verified', 'recovered': infrastructure_recovered},
        profiles=profiles, cluster_id='test', namespace='test', service='api')
    assert result['recovered'] is (None if infrastructure_recovered else False)
    if infrastructure_recovered:
        assert result['reason'] == 'invalid_business_probe_config'
