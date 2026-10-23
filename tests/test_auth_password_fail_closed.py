"""Authentication must never invent a password; execute these tests remotely."""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    web = importlib.import_module('web_app.app')
    for name in (web.AUTH_USER_ENV, web.AUTH_PASSWORD_ENV, web.AUTH_TOKEN_ENV,
                 web.AUTH_ENABLED_ENV, web.AUTH_SESSION_TTL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(web.AUTH_ENABLED_ENV, '1')
    monkeypatch.setenv(web.AUTH_USER_ENV, 'controlled-operator')
    config_file = tmp_path / 'auth_config.json'
    monkeypatch.setattr(web, '_AUTH_CONFIG_FILE', config_file)
    monkeypatch.setattr(web, '_auth_sessions', {})
    return web, config_file, TestClient(web.app)


@pytest.mark.parametrize('configuration', ['absent', 'empty_env', 'empty_file', 'null_file', 'malformed_file'])
def test_missing_password_is_not_replaced_with_an_implicit_credential(isolated_auth, monkeypatch, configuration):
    web, config_file, _ = isolated_auth
    if configuration == 'empty_env':
        # Explicit environment emptiness must override even a populated file.
        config_file.write_text(json.dumps({'password': 'controlled-file-fixture'}))
        monkeypatch.setenv(web.AUTH_PASSWORD_ENV, '')
    elif configuration == 'empty_file':
        config_file.write_text(json.dumps({'password': ''}))
    elif configuration == 'null_file':
        config_file.write_text(json.dumps({'password': None}))
    elif configuration == 'malformed_file':
        config_file.write_text('{controlled malformed fixture')

    # Check only presence so a pre-fix failure cannot print a stored/default
    # credential through pytest's assertion introspection.
    assert not bool(web._auth_password()), 'Unconfigured authentication supplied an implicit password'


@pytest.mark.parametrize('explicit_empty', [False, True])
def test_enabled_auth_without_password_cannot_create_session(isolated_auth, monkeypatch, explicit_empty):
    web, _, client = isolated_auth
    if explicit_empty:
        monkeypatch.setenv(web.AUTH_PASSWORD_ENV, '')
    # Before the fix, use whatever the resolver supplies without embedding or
    # printing its legacy value. After the fix this is an ordinary invalid input.
    attempted_password = web._auth_password() or 'controlled-unconfigured-fixture'
    response = client.post('/api/auth/login', json={
        'username': 'controlled-operator', 'password': attempted_password})
    assert response.status_code == 503
    assert response.json()['detail'] == 'auth_password_not_configured'
    assert web.AUTH_SESSION_COOKIE not in response.cookies
    assert not web._auth_sessions
    assert client.get('/api/nonexistent-protected-auth-fixture').status_code == 401
    assert client.get('/api/auth/me').json()['authenticated'] is False


@pytest.mark.parametrize('source', ['env', 'file'])
def test_explicit_password_still_authenticates_and_wrong_password_does_not(isolated_auth, monkeypatch, source):
    web, config_file, client = isolated_auth
    password = 'controlled-valid-password-fixture'
    if source == 'env':
        monkeypatch.setenv(web.AUTH_PASSWORD_ENV, password)
    else:
        config_file.write_text(json.dumps({'password': password}))
    rejected = client.post('/api/auth/login', json={
        'username': 'controlled-operator', 'password': 'controlled-wrong-password-fixture'})
    assert rejected.status_code == 401
    assert not web._auth_sessions
    accepted = client.post('/api/auth/login', json={
        'username': 'controlled-operator', 'password': password})
    assert accepted.status_code == 200
    assert accepted.json()['auth_enabled'] is True
    assert web.AUTH_SESSION_COOKIE in accepted.cookies
    assert client.get('/api/auth/me').json()['authenticated'] is True
    # A nonexistent protected route avoids external tools while proving the
    # middleware admitted the authenticated session to routing.
    assert client.get('/api/nonexistent-protected-auth-fixture').status_code == 404


def test_missing_password_keeps_explicit_bearer_auth_available(isolated_auth, monkeypatch):
    web, _, client = isolated_auth
    monkeypatch.setenv(web.AUTH_TOKEN_ENV, 'controlled-token-fixture')
    headers = {'Authorization': 'Bearer controlled-token-fixture'}
    assert client.get('/api/nonexistent-protected-auth-fixture', headers=headers).status_code == 404
    me = client.get('/api/auth/me', headers=headers).json()
    assert me['auth_enabled'] is True
    assert me['authenticated'] is True
    assert me['username'] == 'token'
    assert not web._auth_sessions


def test_auth_disabled_stays_compatible_without_password_or_session(isolated_auth, monkeypatch):
    web, _, client = isolated_auth
    monkeypatch.setenv(web.AUTH_ENABLED_ENV, '0')
    response = client.post('/api/auth/login', json={})
    assert response.status_code == 200
    assert response.json()['auth_enabled'] is False
    assert web.AUTH_SESSION_COOKIE not in response.cookies
    assert not web._auth_sessions
    assert client.get('/api/auth/me').json()['authenticated'] is True
    assert client.get('/api/nonexistent-protected-auth-fixture').status_code == 404
