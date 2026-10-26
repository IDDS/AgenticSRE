"""Scoped HTTP recovery probes with optional bounded business JSON assertions."""
import json
import hashlib
import math
import re
import time
from decimal import Decimal, Context, DecimalException
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError as ResponseReadError


_PROFILE_FIELDS = {'url', 'cluster_id', 'namespace', 'service', 'samples', 'interval_seconds',
                   'timeout_seconds', 'max_header_latency_ms', 'expected_status', 'json_assertions',
                   'max_body_bytes', 'max_response_latency_ms'}
VERIFICATION_CONTRACT_VERSION = 'recovery-verification/v1'


def valid_verification_fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r'verification:v1:[0-9a-f]{64}', value) is not None


def _assertions(profile):
    if 'json_assertions' not in profile:
        if {'max_body_bytes', 'max_response_latency_ms'} & profile.keys():
            raise ValueError('Body settings require assertions')
        return None
    assertions = profile['json_assertions']
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 16:
        raise ValueError('Invalid assertions')
    for assertion in assertions:
        if not isinstance(assertion, dict) or not isinstance(assertion.get('path'), list):
            raise ValueError('Invalid assertion path')
        parts = assertion['path']
        if len(parts) > 16 or any(not (
                isinstance(part, str) and len(part) <= 128 or type(part) is int and 0 <= part <= 10000)
                for part in parts):
            raise ValueError('Invalid assertion path')
        if assertion.get('op') == 'non_empty' and set(assertion) == {'path', 'op'}:
            continue
        if assertion.get('op') != 'equals' or set(assertion) != {'path', 'op', 'value'}:
            raise ValueError('Invalid assertion operator')
        value = assertion['value']
        if not (value is None or type(value) is bool or
                isinstance(value, str) and len(value) <= 512 or
                type(value) is int and abs(value) <= 2 ** 63 or
                type(value) is float and math.isfinite(value)):
            raise ValueError('Invalid assertion value')
    return [{**assertion, 'path': list(assertion['path'])} for assertion in assertions]


def _profile_settings(profile):
    if set(profile) - _PROFILE_FIELDS:
        raise ValueError('Unknown probe setting')
    url = profile['url']
    if not isinstance(url, str):
        raise ValueError('Invalid URL')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Invalid URL')
    settings = {'url': url, 'samples': profile.get('samples', 3),
                'interval': profile.get('interval_seconds', 2), 'timeout': profile.get('timeout_seconds', 3),
                'limit': profile.get('max_header_latency_ms', 1000), 'expected': profile.get('expected_status', 200),
                'assertions': _assertions(profile), 'max_bytes': profile.get('max_body_bytes', 65536)}
    settings['response_limit'] = profile.get('max_response_latency_ms', settings['limit'])
    if type(settings['samples']) is not int or not 2 <= settings['samples'] <= 5:
        raise ValueError('Invalid sample count')
    if type(settings['expected']) is not int or not 200 <= settings['expected'] <= 299:
        raise ValueError('Invalid expected status')
    if type(settings['max_bytes']) is not int or not 1 <= settings['max_bytes'] <= 65536:
        raise ValueError('Invalid body limit')
    for key, low, high in [('interval', 1, 10), ('timeout', .1, 5), ('limit', 1, 10000), ('response_limit', 1, 10000)]:
        value = settings[key]
        if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
            raise ValueError('Invalid timing')
    return settings


def _number(value):
    return str(Decimal(str(value)).normalize(Context(prec=64))) if value else '0'


def _compile_contract(*, profiles, cluster_id, namespace, service):
    metadata = {'verification_contract_version': VERIFICATION_CONTRACT_VERSION,
                'verification_fingerprint': '', 'verification_contract_complete': False,
                'verification_level': 'unknown'}
    scope = {'cluster_id': cluster_id or '', 'namespace': namespace, 'service': service}
    if not all(isinstance(value, str) and len(value) <= 512 for value in scope.values()):
        return {**metadata, 'verification_contract_reason': 'invalid_business_probe_config'}, None
    try:
        if not isinstance(profiles, list):
            raise ValueError('Invalid profile collection')
        settings = None
        payload = {'engine': VERIFICATION_CONTRACT_VERSION, 'scope': scope, 'level': 'infrastructure'}
        if profiles:
            matches = [p for p in profiles if isinstance(p, dict) and service and
                       p.get('service') == service and p.get('namespace') == namespace and
                       p.get('cluster_id', '') == (cluster_id or '')]
            if len(matches) != 1:
                reason = 'ambiguous_business_probe' if matches else 'business_probe_not_configured_for_target'
                return {**metadata, 'verification_contract_reason': reason}, None
            settings = _profile_settings(matches[0])
            payload.update(level='json_assertions' if settings['assertions'] is not None else 'http_headers',
                           url=settings['url'], samples=settings['samples'], expected=settings['expected'],
                           interval=_number(settings['interval']), timeout=_number(settings['timeout']),
                           header_limit=_number(settings['limit']))
            if settings['assertions'] is not None:
                assertions = []
                for assertion in settings['assertions']:
                    row = dict(assertion)
                    if row['op'] == 'equals' and type(row['value']) in (int, float):
                        row['value'] = {'json_number': _number(row['value'])}
                    assertions.append(row)
                payload.update(max_bytes=settings['max_bytes'], response_limit=_number(settings['response_limit']),
                               assertions=sorted(assertions, key=lambda row: json.dumps(row, sort_keys=True)))
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        return {**metadata, 'verification_contract_complete': True, 'verification_level': payload['level'],
                'verification_fingerprint': 'verification:v1:' + hashlib.sha256(encoded).hexdigest()}, settings
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        return {**metadata, 'verification_contract_reason': 'invalid_business_probe_config'}, None


def verification_contract(*, profiles, cluster_id, namespace, service):
    """Describe the current acceptance contract without exposing its URL or assertion values."""
    metadata, _ = _compile_contract(profiles=profiles, cluster_id=cluster_id, namespace=namespace, service=service)
    return metadata


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _finite_number(text):
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError('Nonfinite JSON number')
    return value


def _reject_constant(text):
    raise ValueError('Nonstandard JSON constant')


def _evaluate_json(body, assertions):
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant, parse_float=_finite_number)
    except DecimalException:
        return {'passed': None, 'assertions_passed': None, 'error': 'json_number_limit'}
    except (UnicodeError, ValueError):
        return {'passed': False, 'assertions_passed': False, 'error': 'invalid_json_document'}
    except RecursionError:
        return {'passed': None, 'assertions_passed': None, 'error': 'json_structure_limit'}
    stack, nodes = [(value, 0)], 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 10000 or depth > 32:
            return {'passed': None, 'assertions_passed': None, 'error': 'json_structure_limit'}
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    outcomes = []
    missing = object()
    for index, assertion in enumerate(assertions):
        actual = value
        for part in assertion['path']:
            if isinstance(actual, dict) and isinstance(part, str):
                actual = actual.get(part, missing)
            elif isinstance(actual, list) and type(part) is int and part < len(actual):
                actual = actual[part]
            else:
                actual = missing
            if actual is missing:
                break
        if assertion['op'] == 'non_empty':
            passed = isinstance(actual, (str, list, dict)) and len(actual) > 0
        else:
            expected = assertion['value']
            if type(actual) in (int, Decimal) and type(expected) in (int, float):
                passed = Decimal(str(actual)) == Decimal(str(expected))
            else:
                passed = actual is not missing and type(actual) is type(expected) and actual == expected
        outcomes.append({'index': index + 1, 'op': assertion['op'], 'passed': passed})
    passed = all(item['passed'] for item in outcomes)
    return {'passed': passed, 'assertions_passed': passed, 'assertion_results': outcomes,
            **({} if passed else {'error': 'json_assertion_mismatch'})}


def _verify_body(response, *, assertions, max_bytes, before, latency_limit):
    unknown = {'passed': None, 'assertions_passed': None, 'body_bytes': 0}
    # No decompression of untrusted bodies; do not turn compressed bytes into a success.
    if response.headers.get('Content-Encoding', 'identity').strip().lower() != 'identity':
        return {**unknown, 'error': 'unsupported_content_encoding'}
    length = response.headers.get('Content-Length', '')
    if length.isdigit() and (len(length) > 10 or int(length) > max_bytes):
        return {**unknown, 'error': 'body_size_limit'}
    reader = getattr(response.raw, 'read1', None)
    if not callable(reader):
        return {**unknown, 'error': 'bounded_body_reader_unavailable'}
    body = bytearray()
    while True:
        elapsed = (time.monotonic() - before) * 1000
        if elapsed > latency_limit:
            return {'passed': False, 'assertions_passed': None, 'body_bytes': len(body),
                    'error': 'response_latency_exceeded', 'response_latency_ms': round(elapsed, 3)}
        chunk = reader(min(4096, max_bytes - len(body) + 1), decode_content=False)
        body.extend(chunk)
        if len(body) > max_bytes:
            return {**unknown, 'body_bytes': len(body), 'error': 'body_size_limit'}
        if not chunk:
            break
    elapsed = (time.monotonic() - before) * 1000
    if elapsed > latency_limit:
        return {'passed': False, 'assertions_passed': None, 'body_bytes': len(body),
                'error': 'response_latency_exceeded', 'response_latency_ms': round(elapsed, 3)}
    return {**_evaluate_json(body, assertions), 'body_bytes': len(body),
            'response_latency_ms': round(elapsed, 3)}


def verify_business_recovery(infrastructure, *, profiles, cluster_id, namespace, service):
    result = dict(infrastructure)
    result["verification_scope"] = "infrastructure"
    contract, settings = _compile_contract(profiles=profiles, cluster_id=cluster_id, namespace=namespace, service=service)
    result.update(contract)
    eligible = (result.get("status") == "verified" and result.get("recovered") is True) or (
        result.get("reason") == "business_probe_required" and result.get("action_completed") is True)
    if not eligible:
        return result
    if not contract['verification_contract_complete']:
        return {**result, "status": "unknown", "recovered": None,
                "reason": contract['verification_contract_reason']}
    if settings is None:
        return result
    url, samples, interval, timeout, limit, expected, assertions, max_bytes, response_limit = (
        settings[key] for key in ('url', 'samples', 'interval', 'timeout', 'limit', 'expected', 'assertions', 'max_bytes', 'response_limit'))

    observations = []
    started = time.monotonic()
    outcome = True
    reason = "business_probe_passed"
    level = 'json_assertions' if assertions is not None else 'http_headers'
    # Reuse connections within this single scoped observation window only.
    with requests.Session() as session:
        session.trust_env = False
        for index in range(samples):
            if index:
                time.sleep(interval)
            before = time.monotonic()
            try:
                with session.get(url, timeout=timeout, allow_redirects=False, stream=True,
                                 headers={'Accept-Encoding': 'identity'} if assertions is not None else None) as response:
                    elapsed_ms = (time.monotonic() - before) * 1000
                    passed = response.status_code == expected and elapsed_ms <= limit
                    observation = {"sample": index + 1, "at": time.time(),
                        "status_code": response.status_code, "header_latency_ms": round(elapsed_ms, 3),
                        "passed": passed}
                    if passed and assertions is not None:
                        observation.update(_verify_body(response, assertions=assertions, max_bytes=max_bytes,
                                                        before=before, latency_limit=response_limit))
                    observations.append(observation)
                if observation['passed'] is not True:
                    outcome = observation['passed']
                    reason = 'business_probe_incomplete' if outcome is None else 'business_probe_failed'
                    break
            except (requests.RequestException, ResponseReadError, OSError):
                observations.append({"sample": index + 1, "at": time.time(),
                                     "passed": None, "error": "probe_unavailable"})
                outcome, reason = None, "business_probe_unavailable"
                break
    probe = {"type": "business_http", "cluster_id": cluster_id or "", "namespace": namespace,
             "service": service, "required_samples": samples, "interval_seconds": interval,
             "expected_status": expected, "max_header_latency_ms": limit,
             'validation_level': level, 'assertion_count': len(assertions or []),
             **({'max_body_bytes': max_bytes, 'max_response_latency_ms': response_limit} if assertions is not None else {}),
             "elapsed_seconds": round(time.monotonic() - started, 3), "observations": observations,
             "recovered": outcome}
    return {**result, "infrastructure_recovered": result.get("recovered"),
            "verification_scope": "infrastructure_and_business_http",
            'business_validation': level,
            "status": "unknown" if outcome is None else "verified",
            "recovered": outcome, "reason": reason,
            "checks": [*(result.get("checks") or []), probe]}
