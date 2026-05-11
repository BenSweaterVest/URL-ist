"""Pytest configuration — set CONFIG_FILE before importing the app."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

_cfg_dir = Path(tempfile.mkdtemp(prefix="penguinnest_console_test_"))
_cfg_path = _cfg_dir / "config.json"
_cfg_path.write_text("{}", encoding="utf-8")
os.environ["CONFIG_FILE"] = str(_cfg_path)

import main as main_module  # noqa: E402


@pytest.fixture
def config_path() -> Path:
    return Path(os.environ["CONFIG_FILE"])


@pytest.fixture(autouse=True)
def reset_app_state(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test: empty config, clear login rate-limit buckets."""
    monkeypatch.setattr(main_module, "CONFIG_FILE", config_path, raising=False)
    config_path.write_text("{}", encoding="utf-8")
    main_module._login_attempts.clear()
    main_module._load_config()
    yield
    main_module._login_attempts.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main_module.app)


@pytest.fixture
def configured_client(client: TestClient) -> TestClient:
    body = {
        "cf_api_token": "cfat_test",
        "cf_account_id": "a" * 32,
        "cf_zone_id": "b" * 32,
        "npm_url": "http://127.0.0.1:81",
        "npm_email": "admin@example.com",
        "npm_password": "npmsecret",
        "npm_cert_id": 2,
        "domain": "example.com",
        "short_domain": "",
        "cf_list_name": "shortlinks",
        "console_password": "testpass12",
        "console_password_confirm": "testpass12",
    }
    r_cfg = client.post("/api/config", json=body)
    assert r_cfg.status_code == 200, r_cfg.text
    assert client.post("/api/auth/login", json={"password": "testpass12"}).status_code == 200
    return client


@pytest.fixture
def csrf_token(configured_client: TestClient) -> str:
    """CSRF token for authenticated unsafe requests in tests."""
    r = configured_client.get("/api/auth/status")
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token", "")
    assert token, "csrf_token missing from /api/auth/status after login"
    return str(token)


@pytest.fixture
def csrf_headers(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token}
