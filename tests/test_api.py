"""API behaviour tests (no real Cloudflare/NPM — only local config + auth)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

import main as main_module

_LINK_ITEM_ID = "a" * 32


def test_health_includes_version(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["version"] == main_module.__version__
    assert data["session_secret_source"] in ("env", "file", "runtime")


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
        "console_password": "longenough12",
        "console_password_confirm": "longenough12",
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
        "console_password": "longenough12",
        "console_password_confirm": "longenough12",
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
        "console_password": "securepass12",
        "console_password_confirm": "securepass12",
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
        "console_password": "ratepass1234",
        "console_password_confirm": "ratepass1234",
    }
    assert client.post("/api/config", json=body).status_code == 200
    for _ in range(main_module._LOGIN_RATE_MAX):
        r = client.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401
    blocked = client.post("/api/auth/login", json={"password": "wrong"})
    assert blocked.status_code == 429


def test_csrf_enforced_on_unsafe_methods(
    configured_client: TestClient, csrf_headers: dict[str, str]
) -> None:
    """Unsafe method without CSRF should 403; with CSRF should pass auth layer."""
    # Without CSRF token -> blocked by middleware
    r = configured_client.post(
        "/api/dns", json={"name": "x", "content": "10.0.0.1", "proxied": False}
    )
    assert r.status_code == 403, r.text

    # With CSRF token -> passes CSRF; request may still fail because Cloudflare isn't mocked
    r2 = configured_client.post(
        "/api/dns",
        json={"name": "x", "content": "10.0.0.1", "proxied": False},
        headers=csrf_headers,
    )
    assert r2.status_code != 403, r2.text


def test_preflight_authenticated_stub(
    monkeypatch: pytest.MonkeyPatch, configured_client: TestClient
) -> None:
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


@pytest.fixture
def links_ready(monkeypatch: pytest.MonkeyPatch, configured_client: TestClient) -> TestClient:
    async def fake_get_list_id() -> str:
        return "list" + ("0" * 28)

    monkeypatch.setattr(main_module, "get_list_id", fake_get_list_id)
    main_module.config.short_domain = "short.example.com"
    return configured_client


def test_create_link_default_302(
    links_ready: TestClient,
    csrf_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "POST" and "/items" in path:
            captured["body"] = kwargs.get("json")
            return {"result": [{"id": _LINK_ITEM_ID}]}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.post(
        "/api/links",
        json={"slug": "ha", "target": "https://home.example.com"},
        headers=csrf_headers,
    )
    assert r.status_code == 201, r.text
    body = captured["body"]
    assert body[0]["redirect"]["status_code"] == 302
    assert body[0]["redirect"]["source_url"] == "https://short.example.com/ha"


def test_update_link_preserves_status_code_and_source_url(
    links_ready: TestClient,
    csrf_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recreate must keep the stored source URL host, not config.short_domain."""
    recreated: dict = {}
    original_source = "https://legacy-short.example.com/ha"
    main_module.config.short_domain = "short.example.com"

    get_paths: list[str] = []

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "GET" and f"/items/{_LINK_ITEM_ID}" in path:
            get_paths.append(path)
            return {
                "result": {
                    "id": _LINK_ITEM_ID,
                    "redirect": {
                        "source_url": original_source,
                        "target_url": "https://old.example.com",
                        "status_code": 301,
                    },
                },
            }
        if method == "DELETE" and "/items" in path:
            return {"success": True}
        if method == "POST" and "/items" in path:
            recreated["body"] = kwargs.get("json")
            return {"result": [{"id": "b" * 32}]}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.put(
        f"/api/links/{_LINK_ITEM_ID}",
        json={"target": "https://new.example.com"},
        headers=csrf_headers,
    )
    assert r.status_code == 200, r.text
    assert recreated["body"][0]["redirect"]["status_code"] == 301
    assert recreated["body"][0]["redirect"]["target_url"] == "https://new.example.com"
    assert recreated["body"][0]["redirect"]["source_url"] == original_source
    assert len(get_paths) == 1
    assert get_paths[0].endswith(f"/items/{_LINK_ITEM_ID}")


def test_update_link_can_change_status_code(
    links_ready: TestClient,
    csrf_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recreated: dict = {}

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "GET" and f"/items/{_LINK_ITEM_ID}" in path:
            return {
                "result": {
                    "id": _LINK_ITEM_ID,
                    "redirect": {
                        "source_url": "https://short.example.com/ha",
                        "target_url": "https://old.example.com",
                        "status_code": 302,
                    },
                },
            }
        if method == "DELETE" and "/items" in path:
            return {"success": True}
        if method == "POST" and "/items" in path:
            recreated["body"] = kwargs.get("json")
            return {"result": [{"id": "b" * 32}]}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.put(
        f"/api/links/{_LINK_ITEM_ID}",
        json={"target": "https://new.example.com", "status_code": 301},
        headers=csrf_headers,
    )
    assert r.status_code == 200, r.text
    assert recreated["body"][0]["redirect"]["status_code"] == 301


def test_save_config_rejects_invalid_integration_url(client: TestClient, config_path: Path) -> None:
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
        "console_password": "longenough12",
        "console_password_confirm": "longenough12",
        "dockge_url": "javascript:alert(1)",
    }
    r = client.post("/api/config", json=body)
    assert r.status_code == 422


def test_list_links_truncated_flag(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_list_id() -> str:
        return "list" + ("0" * 28)

    async def fake_fetch_list_items(list_id: str) -> tuple[list, bool]:  # noqa: ARG001
        return ([{"id": _LINK_ITEM_ID, "redirect": {}}] * 500, True)

    monkeypatch.setattr(main_module, "get_list_id", fake_get_list_id)
    monkeypatch.setattr(main_module, "_cf_fetch_list_items", fake_fetch_list_items)
    r = configured_client.get("/api/links")
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is True
    assert len(data["items"]) == 500


def test_cloudflare_error_detail_prefix(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        raise HTTPException(status_code=403, detail="Cloudflare: Invalid API token")

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = configured_client.get("/api/dns")
    assert r.status_code == 403
    assert r.json()["detail"] == "Cloudflare: Invalid API token"


def test_list_dns_paginates_all_pages(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        page = int((kwargs.get("params") or {}).get("page", 1))
        calls.append(page)
        if page == 1:
            return {
                "result": [
                    {"id": "1" * 32, "name": "a.example.com", "type": "A", "content": "1.2.3.4"}
                ],
                "result_info": {"page": 1, "per_page": 100, "total_pages": 2},
            }
        return {
            "result": [
                {"id": "2" * 32, "name": "b.example.com", "type": "A", "content": "1.2.3.5"}
            ],
            "result_info": {"page": 2, "per_page": 100, "total_pages": 2},
        }

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = configured_client.get("/api/dns")
    assert r.status_code == 200
    data = r.json()
    assert len(data["records"]) == 2
    assert data["truncated"] is False
    assert calls == [1, 2]


def test_list_dns_includes_npm_proxy_domains(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        return {
            "result": [
                {"id": "1" * 32, "name": "a.example.com", "type": "A", "content": "1.2.3.4"}
            ],
            "result_info": {"page": 1, "per_page": 100, "total_pages": 1},
        }

    async def fake_npm_request(method: str, path: str, **kwargs):  # noqa: ARG001
        assert path == "/api/nginx/proxy-hosts"
        return [{"domain_names": ["app.example.com", "app2.example.com"]}]

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    monkeypatch.setattr(main_module, "npm_request", fake_npm_request)
    r = configured_client.get("/api/dns")
    assert r.status_code == 200
    data = r.json()
    assert data["npm_proxy_domains"] == ["app.example.com", "app2.example.com"]


def test_create_link_accepts_301(
    links_ready: TestClient,
    csrf_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "POST" and "/items" in path:
            captured["body"] = kwargs.get("json")
            return {"result": [{"id": _LINK_ITEM_ID}]}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.post(
        "/api/links",
        json={"slug": "x", "target": "https://example.com", "status_code": 301},
        headers=csrf_headers,
    )
    assert r.status_code == 201, r.text
    assert captured["body"][0]["redirect"]["status_code"] == 301


def test_metrics_endpoint(configured_client: TestClient) -> None:
    r = configured_client.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "http_requests_total" in data
    assert "reconcile_runs_total" in data


def test_config_export_blocked_before_setup(client: TestClient) -> None:
    assert client.get("/api/config/export").status_code == 503


def test_invalid_cf_id_rejected(
    configured_client: TestClient, csrf_headers: dict[str, str]
) -> None:
    r = configured_client.delete("/api/dns/not-a-valid-id", headers=csrf_headers)
    assert r.status_code == 400
    assert "32-character" in r.json()["detail"]


def test_npm_metadata_url_rejected_in_config_test(client: TestClient) -> None:
    body = {
        "cf_api_token": "x",
        "cf_account_id": "a" * 32,
        "cf_zone_id": "b" * 32,
        "npm_url": "http://169.254.169.254/",
        "npm_email": "a@b.com",
        "npm_password": "p",
    }
    r = client.post("/api/config/test", json=body)
    assert r.status_code == 422


def test_batch_import_item_cap(configured_client: TestClient, csrf_headers: dict[str, str]) -> None:
    items = [
        {
            "subdomain": f"s{i}",
            "forward_host": "10.0.0.1",
            "forward_port": 8080,
        }
        for i in range(101)
    ]
    r = configured_client.post(
        "/api/services/batch",
        json={"items": items, "dry_run": True},
        headers=csrf_headers,
    )
    assert r.status_code == 422


def test_auth_login_env_migration_preserves_integration_urls(
    client: TestClient, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONSOLE_PASSWORD env + no hash: first login writes config.json and keeps integration URLs."""
    monkeypatch.setenv("CONSOLE_PASSWORD", "bootstrappass12")
    config_path.write_text(
        json.dumps(
            {
                "cf_api_token": "cfat_test",
                "cf_account_id": "a" * 32,
                "cf_zone_id": "b" * 32,
                "npm_url": "http://127.0.0.1:81",
                "npm_email": "admin@example.com",
                "npm_password": "npmsecret",
                "npm_cert_id": 2,
                "domain": "example.com",
                "uptime_kuma_url": "https://uptime.example.com",
                "homepage_url": "https://home.example.com",
                "dockge_url": "https://dockge.example.com",
                "wiki_url": "https://wiki.example.com",
            }
        ),
        encoding="utf-8",
    )
    main_module._load_config()
    assert not main_module.config.console_password_hash
    assert main_module._console_password_env == "bootstrappass12"

    r = client.post("/api/auth/login", json={"password": "bootstrappass12"})
    assert r.status_code == 200, r.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved.get("dockge_url") == "https://dockge.example.com"
    assert saved.get("wiki_url") == "https://wiki.example.com"
    assert saved.get("uptime_kuma_url") == "https://uptime.example.com"
    assert saved.get("homepage_url") == "https://home.example.com"
    assert saved.get("console_password_hash", "").startswith("scrypt:")
    assert main_module._console_password_env is None


def test_invalid_npm_cert_id_not_configured(client: TestClient, config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "cf_api_token": "cfat_test",
                "cf_account_id": "a" * 32,
                "cf_zone_id": "b" * 32,
                "npm_url": "http://127.0.0.1:81",
                "npm_email": "admin@example.com",
                "npm_password": "npmsecret",
                "npm_cert_id": 0,
                "domain": "example.com",
                "console_password_hash": "scrypt:ignored",
            }
        ),
        encoding="utf-8",
    )
    main_module._load_config()
    # /api/auth/status is always open; /api/config/status now needs login because
    # a console password exists.
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is False
    assert "npm_cert_id" in data["missing_fields"]
    assert data["login_required"] is True
    assert client.get("/api/config/status").status_code == 401


def test_activity_log_trim_drops_oldest(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    path = config_path.parent / "activity.jsonl"
    old_lines = [json.dumps({"ts": f"t{i}", "action": "x", "detail": {}}) for i in range(200)]
    path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "_ACTIVITY_MAX_BYTES", 500)
    monkeypatch.setattr(main_module, "_ACTIVITY_TRIM_TARGET_BYTES", 200)
    main_module.emit_activity("test.event", {"n": 1})
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(kept) < 200
    assert json.loads(kept[-1])["action"] == "test.event"
    assert json.loads(kept[0])["ts"] != "t0"


def _base_config(**extra: object) -> dict:
    body: dict = {
        "cf_api_token": "cfat_test",
        "cf_account_id": "a" * 32,
        "cf_zone_id": "b" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "admin@example.com",
        "npm_password": "npmsecret",
        "npm_cert_id": 2,
        "domain": "example.com",
    }
    body.update(extra)
    return body


def _write_config(config_path: Path, **extra: object) -> None:
    config_path.write_text(json.dumps(_base_config(**extra)), encoding="utf-8")
    main_module._load_config()


def test_partial_config_with_password_requires_login_before_wizard(
    client: TestClient, config_path: Path
) -> None:
    """A stored password gates the wizard even when other settings are missing."""
    _write_config(
        config_path,
        npm_cert_id=0,
        console_password_hash=main_module._hash_console_password("ownerpassword1"),
    )
    assert not main_module.config.is_configured()

    stranger = TestClient(main_module.app)
    assert stranger.get("/api/config").status_code == 401
    assert stranger.post("/api/config", json=_base_config()).status_code == 401

    owner = TestClient(main_module.app)
    assert owner.post("/api/auth/login", json={"password": "ownerpassword1"}).status_code == 200
    status = owner.get("/api/auth/status").json()
    assert status["authenticated"] is True and status["configured"] is False
    # Prefill works, and secrets come back masked.
    cfg = owner.get("/api/config").json()
    assert cfg["cf_account_id"] == "a" * 32
    assert main_module.MASK_CHAR in cfg["cf_api_token"]
    # Fixing the cert ID (sending masked secrets back) completes setup and keeps the token.
    fix = _base_config(cf_api_token=cfg["cf_api_token"], npm_password=cfg["npm_password"])
    r = owner.post("/api/config", json=fix, headers={"X-CSRF-Token": status["csrf_token"]})
    assert r.status_code == 200, r.text
    assert main_module.config.is_configured()
    assert main_module.config.cf_api_token == "cfat_test"
    # The old password still works: nothing was overwritten.
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert main_module._verify_console_password("ownerpassword1", saved["console_password_hash"])


def test_configured_without_password_stays_in_setup_mode(
    client: TestClient, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-only deploy that omits CONSOLE_PASSWORD: only the wizard answers until a password is set."""
    monkeypatch.delenv("CONSOLE_PASSWORD", raising=False)
    _write_config(config_path)
    assert main_module.config.is_configured()
    assert not main_module._login_required()

    assert client.get("/api/links").status_code == 503
    assert client.get("/api/config/export").status_code == 503
    assert client.get("/api/auth/status").json()["console_password_set"] is False
    assert client.get("/api/config").status_code == 200

    # No session exists yet, so the wizard save must not require a CSRF header.
    r = client.post(
        "/api/config",
        json=_base_config(console_password="longenough12", console_password_confirm="longenough12"),
    )
    assert r.status_code == 200, r.text
    assert main_module._login_required()
    assert TestClient(main_module.app).get("/api/links").status_code == 401
    assert client.post("/api/auth/login", json={"password": "longenough12"}).status_code == 200


def test_session_secret_status_reports_key_in_use(configured_client: TestClient) -> None:
    """After the wizard writes a key to disk the process is still on its runtime key."""
    data = configured_client.get("/api/health").json()
    assert data["session_secret_source"] == main_module._ACTIVE_SESSION_SECRET_SOURCE
    if main_module._ACTIVE_SESSION_SECRET_SOURCE == "runtime":
        assert data["session_secret_restart_needed"] is True
    status = configured_client.get("/api/auth/status").json()
    assert status["session_secret_restart_needed"] == data["session_secret_restart_needed"]


def test_activity_log_trim_is_fast(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    import time

    path = config_path.parent / "activity.jsonl"
    line = json.dumps(
        {"ts": "t", "action": "service.created", "detail": {"domain": "x.example.com"}}
    )
    n = main_module._ACTIVITY_MAX_BYTES // (len(line) + 1) + 50
    path.write_bytes(("\n".join([line] * n) + "\n").encode())
    t0 = time.perf_counter()
    main_module.emit_activity("test.event", {"n": 1})
    assert time.perf_counter() - t0 < 2.0
    assert path.stat().st_size <= main_module._ACTIVITY_TRIM_TARGET_BYTES + len(line) + 200
    kept = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(kept[-1])["action"] == "test.event"


def test_create_link_waits_for_cloudflare_operation(
    links_ready: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    polls: list[str] = []
    statuses = iter(["pending", "completed"])

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "POST" and path.endswith("/items"):
            return {"result": {"operation_id": "op123"}}
        if method == "GET" and "/bulk_operations/" in path:
            polls.append(path)
            return {"result": {"id": "op123", "status": next(statuses)}}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    monkeypatch.setattr(main_module, "_CF_LIST_OP_POLL_SEC", 0.0)
    r = links_ready.post(
        "/api/links", json={"slug": "ha", "target": "https://x.example.com"}, headers=csrf_headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["pending"] is False
    assert len(polls) == 2 and polls[0].endswith("/bulk_operations/op123")


def test_create_link_surfaces_failed_operation(
    links_ready: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "POST" and path.endswith("/items"):
            return {"result": {"operation_id": "op1"}}
        if "/bulk_operations/" in path:
            return {"result": {"status": "failed", "error": "duplicate source_url"}}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.post(
        "/api/links", json={"slug": "ha", "target": "https://x.example.com"}, headers=csrf_headers
    )
    assert r.status_code == 502
    assert "duplicate source_url" in r.json()["detail"]


def test_update_link_waits_for_delete_before_create(
    links_ready: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "GET" and f"/items/{_LINK_ITEM_ID}" in path:
            return {
                "result": {
                    "id": _LINK_ITEM_ID,
                    "redirect": {
                        "source_url": "https://short.example.com/ha",
                        "target_url": "https://old",
                    },
                }
            }
        if method == "DELETE":
            calls.append("delete")
            return {"result": {"operation_id": "del"}}
        if method == "POST":
            calls.append("create")
            return {"result": {"operation_id": "add"}}
        if "/bulk_operations/" in path:
            calls.append("poll:" + path.rsplit("/", 1)[-1])
            return {"result": {"status": "completed"}}
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.put(
        f"/api/links/{_LINK_ITEM_ID}", json={"target": "https://new"}, headers=csrf_headers
    )
    assert r.status_code == 200, r.text
    assert calls == ["delete", "poll:del", "create", "poll:add"]


def test_missing_status_code_is_read_as_cloudflare_default_301() -> None:
    assert main_module._redirect_status_from_item({"redirect": {}}) == 301
    assert main_module._redirect_status_from_item({"redirect": {"status_code": 302}}) == 302


def test_save_config_normalizes_domains_and_ids(client: TestClient, config_path: Path) -> None:
    body = _base_config(
        cf_account_id="A" * 32,
        domain="Example.COM.",
        short_domain="https://Short.Example.com/",
        console_password="longenough12",
        console_password_confirm="longenough12",
    )
    assert client.post("/api/config", json=body).status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["cf_account_id"] == "a" * 32
    assert saved["domain"] == "example.com"
    assert saved["short_domain"] == "short.example.com"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cf_account_id", "not-hex"),
        ("cf_zone_id", "b" * 31),
        ("short_domain", "bad_host!"),
        ("dns_target_ip", "npm.lan"),
    ],
)
def test_save_config_rejects_invalid_values(client: TestClient, field: str, value: str) -> None:
    body = _base_config(console_password="longenough12", console_password_confirm="longenough12")
    body[field] = value
    assert client.post("/api/config", json=body).status_code == 422


def test_npm_hostname_requires_dns_target_ip(client: TestClient, config_path: Path) -> None:
    body = _base_config(
        npm_url="http://npm.lan:81",
        console_password="longenough12",
        console_password_confirm="longenough12",
    )
    r = client.post("/api/config", json=body)
    assert r.status_code == 422
    assert "DNS target IP" in r.text
    body["dns_target_ip"] = "10.0.0.9"
    assert client.post("/api/config", json=body).status_code == 200
    assert main_module.config.dns_target == "10.0.0.9"
    assert main_module.config.is_configured()


def test_create_service_uses_dns_target_ip(
    configured_client: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        captured["dns"] = kwargs.get("json")
        return {"result": {"id": "c" * 32}}

    async def fake_npm_request(method: str, path: str, **kwargs):  # noqa: ARG001
        return {}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    monkeypatch.setattr(main_module, "npm_request", fake_npm_request)
    main_module.config.npm_url = "http://npm.lan:81"
    main_module.config.dns_target_ip = "10.0.0.9"
    r = configured_client.post(
        "/api/services",
        json={"subdomain": "app", "forward_host": "10.0.0.5", "forward_port": 80},
        headers=csrf_headers,
    )
    assert r.status_code == 201, r.text
    assert captured["dns"]["content"] == "10.0.0.9"


def test_create_service_rejects_bad_subdomain(
    configured_client: TestClient, csrf_headers: dict[str, str]
) -> None:
    for sub in ("app-", "-app", "a" * 64):
        r = configured_client.post(
            "/api/services",
            json={"subdomain": sub, "forward_host": "10.0.0.5", "forward_port": 80},
            headers=csrf_headers,
        )
        assert r.status_code == 422, sub


def test_unknown_api_route_returns_json_404(configured_client: TestClient) -> None:
    r = configured_client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not found"}
    # The SPA catch-all still serves everything else.
    assert configured_client.get("/some/page").headers["content-type"].startswith("text/html")


def test_set_service_enabled_uses_npm_toggle_endpoints(
    configured_client: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_npm_request(method: str, path: str, **kwargs):  # noqa: ARG001
        calls.append((method, path))
        if path.endswith("/enable"):
            raise HTTPException(status_code=400, detail="NPM: Host is already enabled")
        return True

    monkeypatch.setattr(main_module, "npm_request", fake_npm_request)
    r = configured_client.post(
        "/api/services/7/enabled", json={"enabled": False}, headers=csrf_headers
    )
    assert r.status_code == 200 and r.json()["enabled"] is False
    # Already in the requested state is not an error.
    r2 = configured_client.post(
        "/api/services/7/enabled", json={"enabled": True}, headers=csrf_headers
    )
    assert r2.status_code == 200
    assert calls == [
        ("POST", "/api/nginx/proxy-hosts/7/disable"),
        ("POST", "/api/nginx/proxy-hosts/7/enable"),
    ]


def test_list_dns_flags_npm_unavailable(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        return {"result": [], "result_info": {"total_pages": 1}}

    async def fake_npm_request(method: str, path: str, **kwargs):  # noqa: ARG001
        raise HTTPException(status_code=503, detail="Cannot reach NPM")

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    monkeypatch.setattr(main_module, "npm_request", fake_npm_request)
    data = configured_client.get("/api/dns").json()
    assert data["npm_proxy_domains"] == []
    assert data["npm_proxy_domains_available"] is False


def test_login_rate_limit_forgets_quiet_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(main_module.time, "monotonic", lambda: now[0])
    main_module._record_login_attempt("10.0.0.1")
    assert "10.0.0.1" in main_module._login_attempts
    now[0] += main_module._LOGIN_RATE_WINDOW_SEC + 1
    main_module._record_login_attempt("10.0.0.2")
    assert "10.0.0.1" not in main_module._login_attempts
    assert "10.0.0.2" in main_module._login_attempts


def test_create_link_reports_pending_when_status_read_fails(
    links_ready: TestClient, csrf_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed status poll must not turn an accepted write into an error."""

    async def fake_cf_request(method: str, path: str, **kwargs):  # noqa: ARG001
        if method == "POST" and path.endswith("/items"):
            return {"result": {"operation_id": "op9"}}
        if "/bulk_operations/" in path:
            raise HTTPException(status_code=503, detail="Cannot reach Cloudflare")
        return {"result": []}

    monkeypatch.setattr(main_module, "cf_request", fake_cf_request)
    r = links_ready.post(
        "/api/links", json={"slug": "ha", "target": "https://x.example.com"}, headers=csrf_headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["pending"] is True
