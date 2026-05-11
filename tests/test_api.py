"""API behaviour tests (no real Cloudflare/NPM — only local config + auth)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import main as main_module


def test_health_includes_version(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["version"] == main_module.__version__


def test_wizard_endpoints_without_auth(client: TestClient) -> None:
    assert client.get("/api/config/status").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_config_endpoints_auth_gated_after_configuration() -> None:
    """Before config: open; after config: /api/config* requires session."""
    fresh = TestClient(main_module.app)
    assert fresh.get("/api/config").status_code == 200

    body = {
        "cf_api_token": "x",
        "cf_account_id": "c" * 32,
        "cf_zone_id": "d" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "a@b.com",
        "npm_password": "p",
        "npm_cert_id": 2,
        "domain": "example.com",
        "short_domain": "",
        "cf_list_name": "shortlinks",
        "console_password": "longenough1",
        "console_password_confirm": "longenough1",
    }
    assert fresh.post("/api/config", json=body).status_code == 200

    unauth2 = TestClient(main_module.app)
    assert unauth2.get("/api/config").status_code == 401
    assert unauth2.get("/api/config/status").status_code == 401


def test_protected_route_401_without_session(client: TestClient) -> None:
    body = {
        "cf_api_token": "x",
        "cf_account_id": "c" * 32,
        "cf_zone_id": "d" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "a@b.com",
        "npm_password": "p",
        "npm_cert_id": 2,
        "domain": "example.com",
        "short_domain": "",
        "cf_list_name": "shortlinks",
        "console_password": "longenough1",
        "console_password_confirm": "longenough1",
    }
    assert client.post("/api/config", json=body).status_code == 200
    # New client / no cookie
    fresh = TestClient(main_module.app)
    assert fresh.get("/api/links").status_code == 401


def test_save_config_generates_session_secret(client: TestClient, config_path: Path) -> None:
    body = {
        "cf_api_token": "tok",
        "cf_account_id": "e" * 32,
        "cf_zone_id": "f" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "a@b.com",
        "npm_password": "npm",
        "npm_cert_id": 2,
        "domain": "example.com",
        "short_domain": "",
        "cf_list_name": "shortlinks",
        "console_password": "securepass1",
        "console_password_confirm": "securepass1",
    }
    assert client.post("/api/config", json=body).status_code == 200
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "session_secret" in data
    assert len(data["session_secret"]) >= 16
    assert data["console_password_hash"].startswith("scrypt:")


def test_login_rate_limit(client: TestClient) -> None:
    body = {
        "cf_api_token": "tok",
        "cf_account_id": "1" * 32,
        "cf_zone_id": "2" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "a@b.com",
        "npm_password": "npm",
        "npm_cert_id": 2,
        "domain": "example.com",
        "short_domain": "",
        "cf_list_name": "shortlinks",
        "console_password": "ratepass12",
        "console_password_confirm": "ratepass12",
    }
    assert client.post("/api/config", json=body).status_code == 200
    for _ in range(main_module._LOGIN_RATE_MAX):
        r = client.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401
    blocked = client.post("/api/auth/login", json={"password": "wrong"})
    assert blocked.status_code == 429


def test_csrf_enforced_on_unsafe_methods(configured_client: TestClient, csrf_headers: dict[str, str]) -> None:
    """Unsafe method without CSRF should 403; with CSRF should pass auth layer."""
    # Without CSRF token -> blocked by middleware
    r = configured_client.post("/api/dns", json={"name": "x", "content": "10.0.0.1", "proxied": False})
    assert r.status_code == 403, r.text

    # With CSRF token -> passes CSRF; request may still fail because Cloudflare isn't mocked
    r2 = configured_client.post(
        "/api/dns",
        json={"name": "x", "content": "10.0.0.1", "proxied": False},
        headers=csrf_headers,
    )
    assert r2.status_code != 403, r2.text


def test_preflight_authenticated_stub(monkeypatch: pytest.MonkeyPatch, configured_client: TestClient) -> None:
    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if "/rules/lists" in path and method == "GET" and path.endswith("/rules/lists"):
            return {"result": [{"name": "shortlinks", "id": "abc123"}]}
        if "/dns_records" in path and method == "GET":
            return {
                "result": [
                    {
                        "name": "short.example.com",
                        "proxied": True,
                    },
                ],
            }
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    main_module.config.short_domain = "short.example.com"
    main_module._list_id_cache = None

    r = configured_client.get("/api/links/preflight")
    assert r.status_code == 200
    payload = r.json()
    assert payload["list_exists"] is True
    assert payload["ready"] is True
    assert payload["issues"] == []
