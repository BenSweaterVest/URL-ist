"""
URLer — API Backend
====================
FastAPI application serving the URLer management console at your internal URL
(e.g. urler.yourdomain.com — not publicly exposed).

On first launch the app starts in unconfigured mode and serves a setup wizard
that collects credentials, tests them, and saves them to /data/config.json.
Subsequent starts load from that file automatically — no container restart
needed after the wizard completes.

Configuration priority (highest to lowest):
  1. /data/config.json  — written by the setup wizard or settings form
  2. Environment variables  — useful for scripted/CI deployments
  3. Built-in defaults  — safe values for optional fields

Required fields (all must be supplied via config file or env vars):
  CF_API_TOKEN    Cloudflare API token.  Required permissions:
                    Zone > DNS > Edit
                    Account > Account Filter Lists > Edit
                    Account > Account Rulesets > Edit  (Tofu bootstrap only)
  CF_ACCOUNT_ID   Cloudflare account ID (32-char hex, found in CF dashboard)
  CF_ZONE_ID      Cloudflare zone ID for your domain (32-char hex)
  NPM_URL         Nginx Proxy Manager internal URL  e.g. http://192.168.1.1:81
  NPM_EMAIL       Nginx Proxy Manager admin email
  NPM_PASSWORD    Nginx Proxy Manager admin password
  DOMAIN          Base domain for service records   e.g. example.com

Optional fields (env var names shown, built-in defaults listed):
  SHORT_DOMAIN    Full short-link domain  e.g. short.example.com  (default: "")
  CF_LIST_NAME    Cloudflare list name — must match Tofu bootstrap  (shortlinks)
  NPM_CERT_ID     Wildcard SSL cert ID in NPM                      (2)
  CONFIG_FILE      Override path to config JSON           (/data/config.json)
  SESSION_SECRET   Key for signing login session cookies (>=16 chars). Optional if
                   session_secret is stored in config.json (auto-generated on first save).
  CONSOLE_PASSWORD Plaintext console password for env-only bootstrap; first login
                   persists a hash to config.json (see README).
  SESSION_COOKIE_HTTPS_ONLY  If 1/true, session cookies are not sent over plain HTTP.
  SESSION_COOKIE_SAMESITE    Cookie SameSite: lax|strict|none  (default: lax)
  TRUST_PROXY_HEADERS        If 1/true, trust X-Forwarded-For for rate limiting (only behind a trusted proxy).
  RECONCILE_INTERVAL_SEC     Seconds between background drift scans (default 3600).
  RECONCILE_INITIAL_DELAY_SEC Seconds before first reconcile run (default 120).
  RECONCILE_WEBHOOK_URL      Optional HTTPS URL — POST JSON on reconcile when unmatched_dns or missing_dns > 0.

Manages three resource types:
  Links      Cloudflare Bulk Redirect list items (SHORT_DOMAIN/*)
  DNS        Cloudflare A records for *.DOMAIN
  Services   DNS record + NPM proxy host in one operation
"""

import asyncio
import contextvars
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger("urler")

__version__ = "1.2.0"

CF_BASE = "https://api.cloudflare.com/client/v4"
MASK_CHAR = "•"  # used to mask secrets in GET /api/config responses
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
CONFIG_HISTORY_DIR = Path(os.environ.get("CONFIG_HISTORY_DIR", "/data/config-versions"))


def _config_history_limit() -> int:
    """How many prior config snapshots to keep on disk."""
    raw = os.environ.get("CONFIG_HISTORY_LIMIT", "").strip()
    if not raw:
        return 100
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid CONFIG_HISTORY_LIMIT=%r; using 100", raw)
        return 100


def _write_config_snapshot(prior_path: Path) -> None:
    """Save the previous config.json into CONFIG_HISTORY_DIR with rotation."""
    if not prior_path.exists():
        return
    limit = _config_history_limit()
    if limit <= 0:
        return

    try:
        CONFIG_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        snap = CONFIG_HISTORY_DIR / f"config-{ts}.json"
        # Avoid collision in fast consecutive saves
        if snap.exists():
            snap = CONFIG_HISTORY_DIR / f"config-{ts}-{secrets.token_hex(4)}.json"
        snap.write_text(prior_path.read_text(), encoding="utf-8")
        snap.chmod(0o600)
    except OSError as e:
        logger.warning("Could not write config snapshot: %s", e)
        return

    try:
        snaps = sorted(
            CONFIG_HISTORY_DIR.glob("config-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old in snaps[limit:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_activity_lock = threading.Lock()
_TRASH_LOCK = threading.Lock()
TRASH_MAX = 20
_ACTIVITY_MAX_BYTES = 2_000_000
_ACTIVITY_TRIM_TARGET_BYTES = 1_500_000  # drop oldest lines until at or below this size
_urler_logging_configured = False


def activity_log_path() -> Path:
    return CONFIG_FILE.parent / "activity.jsonl"


def service_trash_path() -> Path:
    return CONFIG_FILE.parent / "service-trash.json"


class _RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = _request_id_ctx.get()
        except LookupError:
            record.request_id = "-"
        return True


def _configure_urler_logging() -> None:
    """Attach structured formatter + request id to the app logger."""
    global _urler_logging_configured
    if _urler_logging_configured:
        return
    lg = logging.getLogger("urler")
    h = logging.StreamHandler()
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    h.addFilter(_RequestIdLogFilter())
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    _urler_logging_configured = True


def _trim_activity_log(path: Path) -> None:
    """Drop oldest events until the log is under the trim target (preserves newest entries)."""
    if not path.exists():
        return
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    while lines:
        body = "\n".join(lines) + "\n"
        if len(body.encode("utf-8")) <= _ACTIVITY_TRIM_TARGET_BYTES:
            path.write_text(body, encoding="utf-8")
            return
        lines.pop(0)
    path.write_text("", encoding="utf-8")


def emit_activity(action: str, detail: dict[str, Any] | None = None) -> None:
    """Append one JSON line to activity.jsonl (best-effort)."""
    line = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": detail or {},
        "request_id": _request_id_ctx.get(),
    }
    try:
        raw = json.dumps(line, ensure_ascii=False) + "\n"
        with _activity_lock:
            path = activity_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _ACTIVITY_MAX_BYTES:
                _trim_activity_log(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(raw)
            try:
                path.chmod(0o600)
            except OSError:
                pass
    except OSError as e:
        logger.warning("activity log write failed: %s", e)


_health_cache: dict[str, Any] | None = None
_health_cache_mono: float = 0.0
_HEALTH_TTL_SEC = 30.0


def invalidate_integrations_health_cache() -> None:
    global _health_cache, _health_cache_mono
    _health_cache = None
    _health_cache_mono = 0.0


_metrics: dict[str, Any] = {
    "http_requests_total": 0,
    "reconcile_runs_total": 0,
    "reconcile_last_unix": None,
}


def _normalize_npm_host_payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return raw
    return None


def _trash_load() -> list[dict[str, Any]]:
    path = service_trash_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _trash_save(rows: list[dict[str, Any]]) -> None:
    path = service_trash_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def trash_prepend_service_entry(entry: dict[str, Any]) -> None:
    with _TRASH_LOCK:
        rows = _trash_load()
        rows.insert(0, entry)
        del rows[TRASH_MAX:]
        _trash_save(rows)


def trash_remove(entry_id: str) -> None:
    with _TRASH_LOCK:
        rows = [r for r in _trash_load() if r.get("id") != entry_id]
        _trash_save(rows)


# If neither SESSION_SECRET nor config.json provides a secret, we use a per-process
# random key. This is secure (not guessable), but sessions will be invalidated on
# restart. Persisting session_secret via the wizard (or setting SESSION_SECRET)
# avoids that.
_RUNTIME_SESSION_SECRET = secrets.token_hex(32)

# Login brute-force mitigation (per client IP, in-process — reset on restart).
_LOGIN_RATE_WINDOW_SEC = 60.0
_LOGIN_RATE_MAX = 12
_login_attempts: dict[str, list[float]] = {}

# When set, logins are verified against this env var until a hash is stored in
# config.json (first successful login persists the hash and clears this path).
_console_password_env: str | None = None

# scrypt parameters for console password hashing.
# n=2**15 exceeds OpenSSL's default scrypt memory cap on some hosts (e.g. GitHub Actions),
# so new hashes use a smaller N. Legacy hashes (no "v2" marker) still verify with n=2**15.
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_N_LEGACY = 2**15
_SCRYPT_N_DEFAULT = 2**13  # ~8 MiB peak — fits typical OpenSSL limits


def _hash_console_password(password: str) -> str:
    """Return a salted scrypt string for storage in config.json (never store plaintext)."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N_DEFAULT,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt:v2:{salt.hex()}:{key.hex()}"


def _verify_console_password(password: str, stored: str) -> bool:
    """Constant-time verify for scrypt hashes produced by _hash_console_password."""
    if not stored.startswith("scrypt:"):
        return False
    try:
        parts = stored.split(":")
        if len(parts) == 4 and parts[1] == "v2":
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            n_cost = _SCRYPT_N_DEFAULT
        elif len(parts) == 3:
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
            n_cost = _SCRYPT_N_LEGACY
        else:
            return False
        key = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=n_cost,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
        )
        return secrets.compare_digest(key, expected)
    except (ValueError, OSError):
        return False


def _session_secret_sources() -> tuple[str, str]:
    """Return (secret, source) where source is 'env', 'file', or 'runtime'."""
    env = os.environ.get("SESSION_SECRET", "").strip()
    if len(env) >= 16:
        return env, "env"
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            fs = str(data.get("session_secret", "")).strip()
            if len(fs) >= 16:
                return fs, "file"
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return _RUNTIME_SESSION_SECRET, "runtime"


def _session_secret() -> str:
    """Secret for SessionMiddleware — env first, then optional key in config.json."""
    secret, source = _session_secret_sources()
    if source == "runtime":
        logger.warning(
            "SESSION_SECRET is not set (or too short) and no session_secret in config.json — "
            "using a per-process random session key (sessions will reset on restart). "
            "Complete the wizard to persist session_secret or set SESSION_SECRET."
        )
    return secret


def _env_int(name: str, default: int, minimum: int) -> int:
    """Parse a positive integer env var with fallback on missing/invalid values."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return max(minimum, default)


def _sanitize_request_id(raw: str) -> str:
    """Strip control chars and cap length so client IDs cannot inject logs/headers."""
    cleaned = re.sub(r"[^\x20-\x7E]", "", raw).strip()
    return cleaned[:64] if cleaned else secrets.token_hex(8)


def _npm_url_ssrf_guard(url: str) -> str:
    """Block cloud-metadata targets; homelab private NPM IPs remain allowed."""
    host = (urlparse(url).hostname or "").lower().strip("[]")
    if not host:
        raise ValueError("NPM URL must include a hostname")
    if host in ("metadata", "metadata.google.internal"):
        raise ValueError("NPM URL cannot target cloud metadata addresses")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_link_local:
        raise ValueError("NPM URL cannot target cloud metadata addresses")
    return url


def _normalize_integration_url(url: str) -> str:
    """Optional integration URLs: empty or http(s) only (blocks javascript: etc.)."""
    v = (url or "").strip()
    if not v:
        return ""
    if not v.startswith(("http://", "https://")):
        raise ValueError("Integration URL must start with http:// or https://")
    return v


def _normalize_npm_url(url: str) -> str:
    v = url.rstrip("/")
    if not v:
        return v
    if not v.startswith(("http://", "https://")):
        raise ValueError("NPM URL must start with http:// or https://")
    return _npm_url_ssrf_guard(v)


def _reconcile_webhook_url() -> str:
    """Return validated reconcile webhook URL or empty string if unset/invalid."""
    hook = os.environ.get("RECONCILE_WEBHOOK_URL", "").strip()
    if not hook:
        return ""
    parsed = urlparse(hook)
    if parsed.scheme != "https" or not parsed.netloc:
        logger.warning("RECONCILE_WEBHOOK_URL must be an https:// URL with a host; ignoring")
        return ""
    return hook


def _ensure_session_secret_in_config(config_data: dict[str, Any], prior: dict[str, Any]) -> None:
    """If no strong secret exists in env, file (prior), or payload, generate one for disk."""
    if len(os.environ.get("SESSION_SECRET", "").strip()) >= 16:
        return
    ss = str(config_data.get("session_secret", "")).strip()
    if len(ss) >= 16:
        return
    prev = str(prior.get("session_secret", "")).strip()
    if len(prev) >= 16:
        config_data["session_secret"] = prev
        return
    config_data["session_secret"] = secrets.token_hex(32)
    logger.info("Generated session_secret and persisted it to config (first-time setup)")


def _session_cookie_https_only() -> bool:
    return os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _session_cookie_same_site() -> str:
    raw = os.environ.get("SESSION_COOKIE_SAMESITE", "lax").strip().lower()
    if raw in ("lax", "strict", "none"):
        return raw
    logger.warning("Invalid SESSION_COOKIE_SAMESITE=%r; using 'lax'", raw)
    return "lax"


def _record_login_attempt(client_ip: str) -> None:
    """Raise 429 when too many login attempts from this IP in the sliding window."""
    now = time.monotonic()
    bucket = _login_attempts.setdefault(client_ip, [])
    bucket[:] = [t for t in bucket if now - t < _LOGIN_RATE_WINDOW_SEC]
    if len(bucket) >= _LOGIN_RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts — try again in a minute.",
        )
    bucket.append(now)


def _trust_proxy_headers() -> bool:
    """Trust X-Forwarded-For only when explicitly enabled."""
    return os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes", "on")


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting.

    If TRUST_PROXY_HEADERS is enabled, uses the **last** IP in X-Forwarded-For
    (the one your trusted reverse proxy appended). The first hop is client-controlled
    and must not be used for rate limiting when proxies do not strip XFF.
    """
    if _trust_proxy_headers():
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return request.client.host if request.client else "unknown"


def _csrf_token(request: Request) -> str:
    """Return the per-session CSRF token (generating one if missing)."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        request.session["csrf_token"] = token
    return str(token)


def _require_csrf(request: Request) -> None:
    """Enforce CSRF header for unsafe methods once authenticated."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    header = request.headers.get("x-csrf-token", "")
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not header or not secrets.compare_digest(header, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def _login_required() -> bool:
    """True when the console should require a successful /api/auth/login session."""
    if not config.is_configured():
        return False
    return bool(config.console_password_hash or _console_password_env)


def _session_authenticated(request: Request) -> bool:
    if not _login_required():
        return True
    return bool(request.session.get("authenticated"))


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class Config:
    """Runtime configuration.  Populated by _load_config() from file + env."""

    cf_api_token: str = ""
    cf_account_id: str = ""
    cf_zone_id: str = ""
    npm_url: str = ""
    npm_email: str = ""
    npm_password: str = ""
    npm_cert_id: int = 2
    domain: str = ""
    short_domain: str = ""
    cf_list_name: str = "shortlinks"
    console_password_hash: str = ""
    uptime_kuma_url: str = ""
    homepage_url: str = ""
    dockge_url: str = ""
    wiki_url: str = ""

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def npm_host(self) -> str:
        """IP/hostname of NPM, parsed from npm_url.  Used for DNS cross-referencing."""
        return urlparse(self.npm_url).hostname or ""

    @property
    def cf_headers(self) -> dict[str, str]:
        """Cloudflare API auth headers, built from the current token."""
        return {
            "Authorization": f"Bearer {self.cf_api_token}",
            "Content-Type": "application/json",
        }

    # ── State helpers ─────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when all required fields are populated."""
        return bool(
            self.cf_api_token
            and self.cf_account_id
            and self.cf_zone_id
            and self.npm_email
            and self.npm_password
            and self.domain
            and self.npm_url
            and self.npm_cert_id >= 1
        )

    def missing_fields(self) -> list[str]:
        """Names of required fields that are currently empty or invalid."""
        required = {
            "cf_api_token": self.cf_api_token,
            "cf_account_id": self.cf_account_id,
            "cf_zone_id": self.cf_zone_id,
            "npm_url": self.npm_url,
            "npm_email": self.npm_email,
            "npm_password": self.npm_password,
            "domain": self.domain,
        }
        missing = [k for k, v in required.items() if not v]
        if self.npm_cert_id < 1:
            missing.append("npm_cert_id")
        return missing


# Global config instance — mutated by _load_config() and save_config().
# Thread/concurrency safety: this app uses a single uvicorn worker (--workers 1)
# so there is only one event loop and no concurrent mutations.  If you switch to
# multiple workers, move state to an external store (Redis, database, etc.).
config = Config()

# ── Per-session caches — reset whenever config changes ────────────────────────

# The Cloudflare list ID is stable for the process lifetime once found.
# It's cleared on config reload in case cf_list_name or credentials changed.
_list_id_cache: str | None = None

# NPM JWT token — refreshed at most once per hour.
# NPM tokens expire after roughly 1 year, but we re-fetch hourly so a
# credential change in settings takes effect within the hour.
_npm_token: str | None = None
_npm_token_expires: float = 0.0

# Shared HTTP clients (connection pooling). Initialized in lifespan.
_cf_client: httpx.AsyncClient | None = None
_npm_client: httpx.AsyncClient | None = None


# ── Config loading ─────────────────────────────────────────────────────────────


def _load_config() -> None:
    """Populate the global config from /data/config.json, then env vars, then defaults.

    Priority (highest wins): config file > environment variable > built-in default.
    Called once at startup and again after the wizard saves new settings.
    Clears all API caches so stale tokens/IDs from previous credentials are dropped.
    """
    global config, _list_id_cache, _npm_token, _npm_token_expires, _console_password_env

    # Reset caches — credentials may have changed
    _list_id_cache = None
    _npm_token = None
    _npm_token_expires = 0.0
    _console_password_env = None

    file_data: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            file_data = json.loads(CONFIG_FILE.read_text())
            logger.info("Loaded config from %s", CONFIG_FILE)
        except Exception as e:
            logger.warning("Could not read config file %s: %s", CONFIG_FILE, e)

    def _get(key: str, default: str = "") -> str:
        """Read key from config file, then env var (UPPER_CASE), then default."""
        return str(file_data.get(key) or os.environ.get(key.upper(), default) or default)

    # Parse npm_cert_id separately — invalid/zero blocks is_configured() (no silent default).
    if "npm_cert_id" in file_data:
        cert_raw = file_data.get("npm_cert_id")
    elif os.environ.get("NPM_CERT_ID") is not None:
        cert_raw = os.environ.get("NPM_CERT_ID")
    else:
        cert_raw = "2"
    try:
        npm_cert_id = int(cert_raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        logger.error(
            "NPM_CERT_ID=%r is not a valid integer — set npm_cert_id >= 1 in config or NPM_CERT_ID env",
            cert_raw,
        )
        npm_cert_id = 0
    else:
        if npm_cert_id < 1:
            logger.error(
                "NPM_CERT_ID=%s is invalid (must be >= 1 — find your wildcard cert ID in NPM → SSL Certificates). "
                "Service creation is disabled until config.json or NPM_CERT_ID is fixed.",
                npm_cert_id,
            )
            npm_cert_id = 0

    config = Config(
        cf_api_token=_get("cf_api_token"),
        cf_account_id=_get("cf_account_id"),
        cf_zone_id=_get("cf_zone_id"),
        npm_url=_get("npm_url", ""),
        npm_email=_get("npm_email"),
        npm_password=_get("npm_password"),
        npm_cert_id=npm_cert_id,
        domain=_get("domain", ""),
        short_domain=_get("short_domain", ""),
        cf_list_name=_get("cf_list_name", "shortlinks"),
        console_password_hash=_get("console_password_hash", ""),
        uptime_kuma_url=_get("uptime_kuma_url", ""),
        homepage_url=_get("homepage_url", ""),
        dockge_url=_get("dockge_url", ""),
        wiki_url=_get("wiki_url", ""),
    )

    if not config.console_password_hash:
        env_pw = os.environ.get("CONSOLE_PASSWORD", "").strip()
        if env_pw:
            _console_password_env = env_pw


def _mask(value: str) -> str:
    """Return a masked representation of a sensitive string.

    Shows the first and last 4 characters; replaces the middle with bullets.
    Used in GET /api/config so the frontend can display 'something is set'
    without exposing the actual credential.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return MASK_CHAR * len(value)
    return value[:4] + MASK_CHAR * (len(value) - 8) + value[-4:]


def _resolve(proposed: str, current: str) -> str:
    """Return the real credential to use, handling masked-but-unchanged values.

    When the settings form is submitted, sensitive fields may contain the
    masked display value from GET /api/config rather than the real credential.
    If proposed is empty or equals the masked form of current, keep current.
    Otherwise use proposed (the user entered a new value).

    Used by both test_config (so testing from settings modal works with
    pre-populated masked fields) and save_config (so resaving without
    changing a credential does not overwrite it with its masked form).
    """
    if not proposed or proposed == _mask(current):
        return current
    return proposed


# ── Application lifecycle ─────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config, log startup state, verify Cloudflare if configured."""
    global _cf_client, _npm_client, _reconcile_task
    _configure_urler_logging()
    _load_config()
    _cf_client = httpx.AsyncClient(timeout=10.0)
    _npm_client = httpx.AsyncClient(timeout=10.0)
    logger.info("URLer starting")
    if config.is_configured():
        logger.info("  Domain:       %s", config.domain)
        logger.info("  Short domain: %s", config.short_domain)
        logger.info("  CF list:      %s", config.cf_list_name)
        logger.info("  NPM:          %s", config.npm_url)
        try:
            list_id = await get_list_id()
            if list_id:
                logger.info("  CF list ID:   %s (found)", list_id)
            else:
                logger.warning(
                    "  CF list '%s' not found — Tofu bootstrap required", config.cf_list_name
                )
        except Exception as e:
            logger.warning("  Cloudflare connectivity check failed: %s", e)
    else:
        logger.warning("  Not configured — missing: %s", ", ".join(config.missing_fields()))
        logger.warning("  Open http://localhost:8000 in your browser to complete setup.")
    _reconcile_task = asyncio.create_task(_reconcile_loop())
    yield
    if _reconcile_task:
        _reconcile_task.cancel()
        with suppress(asyncio.CancelledError):
            await _reconcile_task
    if _cf_client:
        await _cf_client.aclose()
    if _npm_client:
        await _npm_client.aclose()
    logger.info("URLer shutting down")


app = FastAPI(title="URLer", lifespan=lifespan)

# ── Middleware ────────────────────────────────────────────────────────────────


@app.middleware("http")
async def security_gate(request: Request, call_next):
    """Enforce setup wizard gating and signed-cookie authentication for API calls.

    Non-API routes (SPA + /static) are always served.  /api/health stays open for
    Docker healthchecks.  /api/auth/* is open so the login page can call status/login.

    Unconfigured installs: only /api/config* works (wizard).  After configuration,
    /api/config* and all other functional APIs require a valid session unless no
    console password is configured (legacy / migration only).
    """
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    if path == "/api/health" or path.startswith("/api/auth/"):
        return await call_next(request)

    if not config.is_configured():
        # Wizard-only routes — do not expose /api/config/export or other config/* helpers.
        if path in ("/api/config", "/api/config/status"):
            return await call_next(request)
        if path == "/api/config/test" and request.method == "POST":
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={"detail": "Application not configured. Complete the setup wizard first."},
        )

    if not _session_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
        )

    try:
        _require_csrf(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

    return await call_next(request)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Propagate X-Request-ID for tracing; bump API request metrics."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    rid = _sanitize_request_id(request.headers.get("x-request-id") or "")
    request.state.request_id = rid
    token = _request_id_ctx.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        if path != "/api/health":
            _metrics["http_requests_total"] = int(_metrics.get("http_requests_total", 0)) + 1
        return response
    finally:
        _request_id_ctx.reset(token)


# Session must wrap HTTP middleware that uses request.session (Starlette stacks
# add_middleware in reverse: last registered = outermost = runs first on request).
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    max_age=14 * 24 * 3600,
    same_site=_session_cookie_same_site(),
    https_only=_session_cookie_https_only(),
)


# ── API helpers ───────────────────────────────────────────────────────────────


async def cf_request(method: str, path: str, **kwargs) -> Any:
    """Make an authenticated request to the Cloudflare API.

    Uses credentials from the current global config.  Wraps httpx errors into
    HTTPException so the actual Cloudflare error message reaches the frontend.
    Timeout is explicit at 10s; httpx default is 5s but Cloudflare can be slow
    on write operations.
    """
    url = f"{CF_BASE}{path}"
    if not _cf_client:
        raise HTTPException(status_code=503, detail="HTTP client not initialized")
    try:
        r = await _cf_client.request(method, url, headers=config.cf_headers, **kwargs)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
            errors = body.get("errors", [])
            msg = errors[0]["message"] if errors else e.response.text
        except Exception:
            msg = e.response.text
        logger.error("Cloudflare %s %s → %s: %s", method, path, e.response.status_code, msg)
        raise HTTPException(
            status_code=e.response.status_code, detail=f"Cloudflare: {msg}"
        ) from None
    except httpx.RequestError as e:
        logger.error("Cloudflare unreachable: %s", e)
        raise HTTPException(status_code=503, detail=f"Cannot reach Cloudflare: {str(e)}") from None


_CF_DNS_PAGE_SIZE = 100
_CF_DNS_MAX_PAGES = 100  # up to 10_000 A records
_CF_LIST_ITEMS_PAGE_SIZE = 500
_CF_LIST_ITEMS_MAX_PAGES = 50  # up to 25_000 items


async def _cf_fetch_zone_dns_a_records() -> tuple[list[dict[str, Any]], bool]:
    """Fetch all zone A records, following Cloudflare page-based pagination."""
    all_records: list[dict[str, Any]] = []
    page = 1
    truncated = False
    while page <= _CF_DNS_MAX_PAGES:
        data = await cf_request(
            "GET",
            f"/zones/{config.cf_zone_id}/dns_records",
            params={
                "per_page": _CF_DNS_PAGE_SIZE,
                "page": page,
                "order": "name",
                "type": "A",
            },
        )
        batch = data.get("result", [])
        all_records.extend(batch)
        info = data.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages or not batch:
            break
        page += 1
    else:
        truncated = True
    return all_records, truncated


async def _cf_fetch_list_items(list_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Fetch all bulk-redirect list items (cursor-first, then page-based fallback)."""
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 1
    truncated = False
    path = f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items"
    for _ in range(_CF_LIST_ITEMS_MAX_PAGES):
        params: dict[str, Any] = {"per_page": _CF_LIST_ITEMS_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        else:
            params["page"] = page
        data = await cf_request("GET", path, params=params)
        batch = list(data.get("result") or [])
        if not batch:
            break
        all_items.extend(batch)
        info = data.get("result_info") or {}
        after = (info.get("cursors") or {}).get("after")
        if after:
            cursor = str(after)
            page = 1
            continue
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
        cursor = None
    else:
        truncated = True
    return all_items, truncated


async def _cf_get_list_item(list_id: str, item_id: str) -> dict[str, Any]:
    """Fetch one bulk-redirect list item by ID (avoids paging the full list)."""
    data = await cf_request(
        "GET",
        f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items/{item_id}",
    )
    item = data.get("result")
    if not item or not isinstance(item, dict):
        raise HTTPException(status_code=404, detail="Short link item not found")
    return item


async def get_list_id() -> str | None:
    """Resolve the Cloudflare redirect list ID by name, with in-process caching.

    Returns None if the list does not yet exist (Tofu bootstrap not run).
    Cache is cleared whenever config is reloaded.
    """
    global _list_id_cache
    if _list_id_cache:
        return _list_id_cache
    data = await cf_request("GET", f"/accounts/{config.cf_account_id}/rules/lists")
    for lst in data.get("result", []):
        if lst["name"] == config.cf_list_name:
            _list_id_cache = lst["id"]
            logger.info("Cached CF list ID: %s", _list_id_cache)
            return _list_id_cache
    return None


async def get_npm_token() -> str:
    """Return a valid NPM JWT token, refreshing at most once per hour.

    NPM tokens are long-lived (~1 year default), but we cache for 1 hour so a
    password change takes effect without a container restart.
    Cache is cleared whenever config is reloaded.
    """
    global _npm_token, _npm_token_expires
    now = time.monotonic()
    if _npm_token and now < _npm_token_expires:
        return _npm_token

    if not _npm_client:
        raise HTTPException(status_code=503, detail="HTTP client not initialized")
    try:
        r = await _npm_client.post(
            f"{config.npm_url.rstrip('/')}/api/tokens",
            json={"identity": config.npm_email, "secret": config.npm_password},
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502, detail=f"NPM authentication failed (HTTP {e.response.status_code})"
        ) from None
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503, detail=f"Cannot reach NPM at {config.npm_url}: {str(e)}"
        ) from None
    token = r.json().get("token")
    if not token:
        raise HTTPException(status_code=502, detail="NPM returned success but no token in response")
    _npm_token = token
    _npm_token_expires = now + 3600

    logger.info("NPM token refreshed")
    return token  # local var; _npm_token (global) is also set for cache use


async def npm_request(method: str, path: str, **kwargs) -> Any:
    """Make an authenticated request to the Nginx Proxy Manager API.

    Acquires a token on first call (or after expiry) and wraps errors into
    HTTPException with the actual NPM error message.
    """
    token = await get_npm_token()
    url = f"{config.npm_url.rstrip('/')}{path}"
    if not _npm_client:
        raise HTTPException(status_code=503, detail="HTTP client not initialized")
    try:
        r = await _npm_client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("error", e.response.text)
        except Exception:
            msg = e.response.text
        logger.error("NPM %s %s → %s: %s", method, path, e.response.status_code, msg)
        raise HTTPException(status_code=e.response.status_code, detail=f"NPM: {msg}") from None
    except httpx.RequestError as e:
        logger.error("NPM unreachable: %s", e)
        raise HTTPException(
            status_code=503, detail=f"Cannot reach NPM at {config.npm_url}: {str(e)}"
        ) from None


# ── Input validation helpers ───────────────────────────────────────────────────

_CF_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _validate_cf_id(id_value: str, label: str) -> None:
    """Reject Cloudflare record/item IDs that are not hex strings.

    Cloudflare IDs are 32-character hex strings (e.g. DNS record IDs, list
    item IDs).  Validating them before inserting into URL paths prevents
    path-traversal payloads like '../../users/tokens' from reaching the
    Cloudflare API.
    """
    if not _CF_ID_RE.match(id_value):
        raise HTTPException(
            status_code=400, detail=f"Invalid {label}: must be a 32-character hex string"
        )


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    """Health check. Used by Docker healthcheck and Uptime Kuma.

    Always returns 200 — even when unconfigured — so the container is considered
    healthy while the setup wizard is being completed.
    """
    _, session_source = _session_secret_sources()
    return {
        "status": "ok",
        "service": "console",
        "version": __version__,
        "configured": config.is_configured(),
        "session_secret_source": session_source,
    }


# ── Authentication (session cookie) ─────────────────────────────────────────────


class LoginBody(BaseModel):
    password: str


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Return bootstrap and login state for the SPA (no authentication required)."""
    _, session_source = _session_secret_sources()
    return {
        "configured": config.is_configured(),
        "missing_fields": config.missing_fields(),
        "authenticated": bool(request.session.get("authenticated")),
        "login_required": _login_required(),
        "csrf_token": _csrf_token(request) if request.session.get("authenticated") else "",
        "session_secret_source": session_source,
    }


@app.post("/api/auth/login")
async def auth_login(request: Request, body: LoginBody):
    """Validate the console password and start a signed session."""
    if not config.is_configured():
        raise HTTPException(status_code=400, detail="Application is not configured yet")

    client_ip = _client_ip(request)
    _record_login_attempt(client_ip)

    ok = False
    global _console_password_env

    if config.console_password_hash:
        ok = _verify_console_password(body.password, config.console_password_hash)
    elif _console_password_env is not None:
        ok = secrets.compare_digest(body.password, _console_password_env)
        if ok:
            new_hash = _hash_console_password(body.password)
            snapshot: dict[str, Any] = {
                "cf_api_token": config.cf_api_token,
                "cf_account_id": config.cf_account_id,
                "cf_zone_id": config.cf_zone_id,
                "npm_url": config.npm_url,
                "npm_email": config.npm_email,
                "npm_password": config.npm_password,
                "npm_cert_id": config.npm_cert_id,
                "domain": config.domain,
                "short_domain": config.short_domain,
                "cf_list_name": config.cf_list_name,
                "uptime_kuma_url": config.uptime_kuma_url,
                "homepage_url": config.homepage_url,
                "dockge_url": config.dockge_url,
                "wiki_url": config.wiki_url,
                "console_password_hash": new_hash,
            }
            prior_snap: dict[str, Any] = {}
            if CONFIG_FILE.exists():
                try:
                    prior_snap = json.loads(CONFIG_FILE.read_text())
                    ss = str(prior_snap.get("session_secret", "")).strip()
                    if len(ss) >= 16:
                        snapshot["session_secret"] = ss
                except (OSError, json.JSONDecodeError, TypeError):
                    prior_snap = {}
            _ensure_session_secret_in_config(snapshot, prior_snap)
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(snapshot, indent=2))
            CONFIG_FILE.chmod(0o600)
            _console_password_env = None
            _load_config()
    else:
        raise HTTPException(
            status_code=503,
            detail="No console password is configured — complete the setup wizard",
        )

    if not ok:
        raise HTTPException(status_code=401, detail="Invalid password")

    request.session["authenticated"] = True
    _csrf_token(request)  # ensure token exists for subsequent unsafe requests
    return {"success": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Clear the session cookie."""
    request.session.clear()
    return {"success": True}


# ── Configuration endpoints ───────────────────────────────────────────────────


@app.get("/api/config/status")
async def config_status():
    """Return whether the app is configured.

    Note: the current SPA uses /api/auth/status for bootstrapping. This endpoint
    is kept for compatibility / debugging. Once configured, it requires an
    authenticated session (like other /api/* routes).
    """
    return {
        "configured": config.is_configured(),
        "missing_fields": config.missing_fields(),
    }


@app.get("/api/config")
async def get_config():
    """Return current configuration with sensitive fields masked.

    Used by the settings form to pre-populate fields.  Tokens and passwords
    are replaced with a masked representation (first+last 4 chars visible).
    To change a sensitive field, clear it and enter the new value; the server
    detects unchanged masked values via _resolve() and keeps the existing credential.
    Accessible without auth only during first-run setup; once configured it
    requires an authenticated session.
    """
    return {
        "configured": config.is_configured(),
        "missing_fields": config.missing_fields(),
        "cf_api_token": _mask(config.cf_api_token),
        "cf_account_id": config.cf_account_id,
        "cf_zone_id": config.cf_zone_id,
        "npm_url": config.npm_url,
        "npm_email": config.npm_email,
        "npm_password": _mask(config.npm_password),
        "npm_cert_id": config.npm_cert_id,
        "domain": config.domain,
        "short_domain": config.short_domain,
        "cf_list_name": config.cf_list_name,
        "console_password_set": bool(config.console_password_hash or _console_password_env),
        "uptime_kuma_url": config.uptime_kuma_url,
        "homepage_url": config.homepage_url,
        "dockge_url": config.dockge_url,
        "wiki_url": config.wiki_url,
    }


class ConfigProposal(BaseModel):
    """Configuration submitted by the setup wizard or the settings form.

    Sensitive fields (cf_api_token, npm_password) may arrive pre-populated with
    masked display values from GET /api/config.  The save_config and test_config
    endpoints call _resolve() to detect those masked values and preserve the real
    stored credential rather than overwriting it with bullet characters.

    Non-sensitive fields (domain, npm_url, etc.) are always used as submitted.
    Sending an empty string for these fields is valid and means "clear this value",
    which will cause is_configured() to return False until it is set again.
    """

    cf_api_token: str
    cf_account_id: str
    cf_zone_id: str
    npm_url: str = ""
    npm_email: str
    npm_password: str
    npm_cert_id: int = 2
    domain: str = ""
    short_domain: str = ""
    cf_list_name: str = "shortlinks"
    console_password: str = ""
    console_password_confirm: str = ""
    uptime_kuma_url: str = ""
    homepage_url: str = ""
    dockge_url: str = ""
    wiki_url: str = ""

    @field_validator("npm_cert_id")
    @classmethod
    def validate_cert_id(cls, v: int) -> int:
        if v < 1:
            raise ValueError("NPM cert ID must be >= 1 (0 = no SSL, not supported here)")
        return v

    @field_validator("npm_url")
    @classmethod
    def validate_npm_url(cls, v: str) -> str:
        if not v:
            return v
        return _normalize_npm_url(v)

    @field_validator("uptime_kuma_url", "homepage_url", "dockge_url", "wiki_url")
    @classmethod
    def validate_integration_urls(cls, v: str) -> str:
        return _normalize_integration_url(v)


class ConfigTestProposal(BaseModel):
    cf_api_token: str
    cf_account_id: str
    cf_zone_id: str
    npm_url: str = ""
    npm_email: str
    npm_password: str
    npm_cert_id: int = 2
    domain: str = ""
    short_domain: str = ""
    cf_list_name: str = "shortlinks"

    @field_validator("npm_cert_id")
    @classmethod
    def validate_cert_id(cls, v: int) -> int:
        if v < 1:
            raise ValueError("NPM cert ID must be >= 1 (0 = no SSL, not supported here)")
        return v

    @field_validator("npm_url")
    @classmethod
    def validate_npm_url(cls, v: str) -> str:
        if not v:
            return v
        return _normalize_npm_url(v)


@app.post("/api/config/test")
async def test_config(proposal: ConfigTestProposal):
    """Test Cloudflare and NPM connectivity using the provided credentials.

    Performs a live check against both services without saving anything.
    Returns per-service results so the wizard can give specific feedback.
    Unauthenticated only before first-time setup (/api/config* while unconfigured);
    after configuration, requires an authenticated session like other /api/config routes.
    """
    # Resolve masked fields against current config.  When called from the settings
    # modal, the form contains masked display values from GET /api/config.
    # _resolve() detects those and substitutes the real stored credentials so the
    # test uses values that actually work, not bullet-char placeholders.
    cf_token = _resolve(proposal.cf_api_token, config.cf_api_token)
    npm_pass = _resolve(proposal.npm_password, config.npm_password)

    # Run both checks concurrently — independent operations, no reason to serialize.
    async def _check_cf() -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{CF_BASE}/zones/{proposal.cf_zone_id}/dns_records",
                    headers=headers,
                    params={"per_page": 1},
                )
                r.raise_for_status()
            return {"ok": True, "error": None}
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.json()
                errors = body.get("errors", [])
                msg = errors[0]["message"] if errors else f"HTTP {e.response.status_code}"
            except Exception:
                msg = f"HTTP {e.response.status_code}"
            return {"ok": False, "error": msg}
        except httpx.RequestError as e:
            return {"ok": False, "error": f"Connection failed — is Cloudflare reachable? ({e})"}

    async def _check_npm() -> dict[str, Any]:
        if not proposal.npm_url:
            return {"ok": False, "error": "NPM URL is required"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{proposal.npm_url}/api/tokens",
                    json={"identity": proposal.npm_email, "secret": npm_pass},
                )
                r.raise_for_status()
            return {"ok": True, "error": None}
        except httpx.HTTPStatusError as e:
            return {
                "ok": False,
                "error": f"Authentication failed (HTTP {e.response.status_code}) — check email/password",
            }
        except httpx.RequestError as e:
            return {"ok": False, "error": f"Connection failed — check NPM URL ({e})"}

    cf_result, npm_result = await asyncio.gather(_check_cf(), _check_npm())

    return {
        "cloudflare": cf_result,
        "npm": npm_result,
        "all_ok": cf_result["ok"] and npm_result["ok"],
    }


@app.post("/api/config")
async def save_config(proposal: ConfigProposal):
    """Save configuration to CONFIG_FILE and reload the in-memory config.

    The app does not need to restart — config is applied immediately and all
    API caches (Cloudflare list ID, NPM token) are cleared so fresh credentials
    are used on the next request.

    Masked fields: if a submitted token or password still contains the mask
    character (MASK_CHAR), it is treated as 'unchanged' and the existing value
    is preserved.  This handles the case where the settings form is submitted
    without the user modifying a pre-filled masked field.
    Always accessible regardless of configuration state.
    """
    prior: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            prior = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            prior = {}

    prev_hash = str(prior.get("console_password_hash", "")).strip()
    new_pw = proposal.console_password.strip()
    final_hash = prev_hash

    if new_pw:
        if new_pw != proposal.console_password_confirm.strip():
            raise HTTPException(status_code=400, detail="Console passwords do not match")
        if len(new_pw) < 12:
            raise HTTPException(
                status_code=400,
                detail="Console password must be at least 12 characters",
            )
        final_hash = _hash_console_password(new_pw)

    if not final_hash and _console_password_env is None:
        raise HTTPException(
            status_code=400,
            detail="Console password is required (set it in the wizard, or use CONSOLE_PASSWORD in the environment)",
        )

    config_data = {
        "cf_api_token": _resolve(proposal.cf_api_token, config.cf_api_token),
        "cf_account_id": proposal.cf_account_id,
        "cf_zone_id": proposal.cf_zone_id,
        "npm_url": proposal.npm_url,
        "npm_email": proposal.npm_email,
        "npm_password": _resolve(proposal.npm_password, config.npm_password),
        "npm_cert_id": proposal.npm_cert_id,
        "domain": proposal.domain,
        "short_domain": proposal.short_domain,
        "cf_list_name": proposal.cf_list_name,
        "console_password_hash": final_hash,
        "uptime_kuma_url": proposal.uptime_kuma_url.strip(),
        "homepage_url": proposal.homepage_url.strip(),
        "dockge_url": proposal.dockge_url.strip(),
        "wiki_url": proposal.wiki_url.strip(),
    }

    if final_hash:
        _ensure_session_secret_in_config(config_data, prior)
    else:
        ss = str(prior.get("session_secret", "")).strip()
        if len(ss) >= 16:
            config_data["session_secret"] = ss

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_config_snapshot(CONFIG_FILE)
        CONFIG_FILE.write_text(json.dumps(config_data, indent=2))
        CONFIG_FILE.chmod(0o600)  # credentials — owner read/write only
    except OSError as e:
        raise HTTPException(
            500,
            f"Could not write config to {CONFIG_FILE}: {e}. "
            "Ensure the /data volume is mounted (see compose.yaml).",
        ) from None

    _load_config()
    invalidate_integrations_health_cache()
    logger.info("Configuration saved and reloaded")
    emit_activity("config.updated", {"configured": config.is_configured()})
    return {"success": True, "configured": config.is_configured()}


# ── Short Links ───────────────────────────────────────────────────────────────


@app.get("/api/links")
async def list_links():
    """List all redirect entries in the Cloudflare bulk redirect list.

    Returns {list_exists: False} when the list hasn't been created yet
    (Tofu bootstrap required).  The frontend renders a setup notice rather
    than an error in this case.
    """
    list_id = await get_list_id()
    if not list_id:
        return {"items": [], "list_exists": False}
    items, truncated = await _cf_fetch_list_items(list_id)
    return {
        "items": items,
        "list_exists": True,
        "truncated": truncated,
    }


@app.get("/api/links/preflight")
async def short_links_preflight():
    """Check short-link readiness: bulk redirect list + short-domain DNS (proxied A).

    Intended for the Short Links tab and for operators scripting checks. Does not
    validate account-level ruleset presence (requires extra API scope); Tofu
    bootstrap remains the source of truth for ruleset + list creation.
    """
    list_id = await get_list_id()
    dns_block: dict[str, Any] = {
        "checked": False,
        "found": False,
        "proxied": None,
        "name": (config.short_domain or "").strip().lower(),
        "hint": None,
    }
    name = dns_block["name"]
    if name:
        dns_block["checked"] = True
        try:
            data = await cf_request(
                "GET",
                f"/zones/{config.cf_zone_id}/dns_records",
                params={"name": name, "type": "A", "per_page": 10},
            )
            results = data.get("result", [])
            if results:
                rec = results[0]
                dns_block["found"] = True
                dns_block["proxied"] = rec.get("proxied")
                dns_block["name"] = rec.get("name", name)
                if rec.get("proxied") is False:
                    dns_block["hint"] = (
                        "This A record should be proxied (orange cloud) so Cloudflare "
                        "can intercept requests and apply bulk redirects at the edge."
                    )
            else:
                dns_block["hint"] = (
                    "No A record for this hostname in the zone — run Tofu bootstrap "
                    "or add a proxied A record for the short domain."
                )
        except HTTPException as e:
            dns_block["hint"] = str(e.detail)

    issues: list[str] = []
    if not list_id:
        issues.append(
            "Bulk redirect list not found — run once: cd tofu && tofu init && tofu apply",
        )
    if not config.short_domain:
        issues.append(
            "Short Links Domain is not set — open Settings and set it to e.g. short.example.com."
        )
    elif dns_block["checked"] and not dns_block["found"]:
        issues.append(f"No A record in this zone for {name}.")
    elif dns_block["checked"] and dns_block["found"] and dns_block.get("proxied") is False:
        issues.append("Short domain exists but is DNS-only (grey cloud) — enable proxying.")

    ready = bool(
        list_id
        and config.short_domain
        and dns_block["checked"]
        and dns_block["found"]
        and dns_block.get("proxied") is True,
    )

    return {
        "list_exists": bool(list_id),
        "cf_list_name": config.cf_list_name,
        "short_domain": config.short_domain,
        "dns": dns_block,
        "ready": ready,
        "issues": issues,
    }


ALLOWED_REDIRECT_STATUS_CODES = frozenset({301, 302, 307, 308})
DEFAULT_REDIRECT_STATUS_CODE = 302


def _redirect_status_from_item(item: dict[str, Any]) -> int:
    """Return a valid redirect status from a CF list item, defaulting to 302."""
    try:
        code = int((item.get("redirect") or {}).get("status_code", DEFAULT_REDIRECT_STATUS_CODE))
    except (TypeError, ValueError):
        return DEFAULT_REDIRECT_STATUS_CODE
    return code if code in ALLOWED_REDIRECT_STATUS_CODES else DEFAULT_REDIRECT_STATUS_CODE


class LinkCreate(BaseModel):
    slug: str
    target: str
    status_code: int = DEFAULT_REDIRECT_STATUS_CODE

    @field_validator("status_code")
    @classmethod
    def validate_status_code(cls, v: int) -> int:
        if v not in ALLOWED_REDIRECT_STATUS_CODES:
            raise ValueError("status_code must be 301, 302, 307, or 308")
        return v

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.strip("/").lower()
        if not v:
            raise ValueError("Slug cannot be empty")
        if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", v):
            raise ValueError("Slug must be lowercase alphanumeric, hyphens, or underscores")
        return v

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Target must start with http:// or https://")
        return v


class LinkUpdate(BaseModel):
    target: str
    status_code: int | None = None

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Target must start with http:// or https://")
        return v

    @field_validator("status_code")
    @classmethod
    def validate_status_code(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v not in ALLOWED_REDIRECT_STATUS_CODES:
            raise ValueError("status_code must be 301, 302, 307, or 308")
        return v


@app.post("/api/links", status_code=201)
async def create_link(link: LinkCreate):
    """Add a redirect entry: SHORT_DOMAIN/{slug} → target (302 by default).

    Uses 302 by default so browsers do not cache redirects permanently; edits to the
    destination remain visible after a slug has been visited. Pass status_code for 301/307/308.

    The full source URL is constructed server-side; the frontend only deals in slugs.
    Requires short_domain to be configured (set in settings).
    """
    if not config.short_domain:
        raise HTTPException(
            status_code=400,
            detail="Short domain not configured — set Short Links Domain in settings first",
        )
    list_id = await get_list_id()
    if not list_id:
        raise HTTPException(404, "Shortlink list not found — run Tofu bootstrap first")
    source_url = f"https://{config.short_domain}/{link.slug}"
    data = await cf_request(
        "POST",
        f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
        json=[
            {
                "redirect": {
                    "source_url": source_url,
                    "target_url": link.target,
                    "status_code": link.status_code,
                }
            }
        ],
    )
    logger.info("Created short link: %s → %s", source_url, link.target)
    emit_activity("link.created", {"slug": link.slug, "source_url": source_url})
    return data


@app.delete("/api/links/{item_id}", status_code=200)
async def delete_link(item_id: str):
    """Remove a redirect entry from the Cloudflare list by its item ID."""
    _validate_cf_id(item_id, "item_id")
    list_id = await get_list_id()
    if not list_id:
        raise HTTPException(404, "Shortlink list not found")
    await cf_request(
        "DELETE",
        f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
        json={"items": [{"id": item_id}]},
    )
    logger.info("Deleted short link: %s", item_id)
    emit_activity("link.deleted", {"item_id": item_id})
    return {"success": True}


@app.put("/api/links/{item_id}", status_code=200)
async def update_link(item_id: str, payload: LinkUpdate):
    """Update target URL for an existing short link item.

    Uses a safe replace flow (delete old item, recreate with same slug/new target).
    If recreate fails, attempts to restore the original mapping.
    """
    _validate_cf_id(item_id, "item_id")
    if not config.short_domain:
        raise HTTPException(status_code=400, detail="Short domain not configured")
    list_id = await get_list_id()
    if not list_id:
        raise HTTPException(404, "Shortlink list not found")

    existing = await _cf_get_list_item(list_id, item_id)

    source_url = str((existing.get("redirect") or {}).get("source_url", ""))
    old_target = str((existing.get("redirect") or {}).get("target_url", ""))
    status_code = (
        payload.status_code
        if payload.status_code is not None
        else _redirect_status_from_item(existing)
    )
    # Slug comes from the stored source URL path — do not depend on current
    # config.short_domain (it may have changed since the item was created).
    path = (urlparse(source_url).path or "").strip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        raise HTTPException(status_code=400, detail="Could not resolve slug from source_url")

    # Step 1: remove old mapping
    await cf_request(
        "DELETE",
        f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
        json={"items": [{"id": item_id}]},
    )

    try:
        # Step 2: create updated mapping
        await cf_request(
            "POST",
            f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
            json=[
                {
                    "redirect": {
                        "source_url": source_url,
                        "target_url": payload.target,
                        "status_code": status_code,
                    },
                }
            ],
        )
        logger.info("Updated short link: /%s → %s", slug, payload.target)
        emit_activity("link.updated", {"slug": slug, "item_id": item_id})
        return {"success": True, "slug": slug}
    except HTTPException:
        # Best-effort restore to previous target
        try:
            await cf_request(
                "POST",
                f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
                json=[
                    {
                        "redirect": {
                            "source_url": source_url,
                            "target_url": old_target,
                            "status_code": status_code,
                        },
                    }
                ],
            )
        except Exception as restore_err:
            logger.error(
                "Failed to restore short link /%s after update error: %s", slug, restore_err
            )
        raise


# ── DNS Records ───────────────────────────────────────────────────────────────


def _npm_host_domain_names(host: dict[str, Any]) -> list[str]:
    """All domain names on an NPM proxy host (lowercased)."""
    return [str(x).lower() for x in (host.get("domain_names") or []) if x]


async def _npm_proxy_domain_names() -> set[str]:
    """Domain names with an NPM proxy host (for DNS delete guardrails)."""
    try:
        hosts = await npm_request("GET", "/api/nginx/proxy-hosts")
    except HTTPException:
        return set()
    names: set[str] = set()
    for host in hosts:
        names.update(_npm_host_domain_names(host))
    return names


@app.get("/api/dns")
async def list_dns():
    """List all A records in the zone, ordered by name.

    Only A records are returned. Follows Cloudflare pagination so zones with
    more than 100 A records are fully listed (unless an internal safety cap is hit).
    Includes npm_proxy_domains so the UI can warn before deleting records tied to services.
    """
    records, truncated = await _cf_fetch_zone_dns_a_records()
    npm_domains = await _npm_proxy_domain_names()
    return {
        "records": records,
        "truncated": truncated,
        "npm_proxy_domains": sorted(npm_domains),
    }


class DNSCreate(BaseModel):
    # Accepts bare subdomain ('myservice') or FQDN — Cloudflare normalises both.
    name: str
    content: str = ""  # default resolved to config.npm_host at request time
    proxied: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Name cannot be empty")
        return v

    @field_validator("content")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        if not v:
            return v  # empty string handled at endpoint level (defaults to npm_host)
        try:
            ipaddress.IPv4Address(v.strip())
        except ValueError:
            raise ValueError("Content must be a valid IPv4 address") from None
        return v.strip()


@app.post("/api/dns", status_code=201)
async def create_dns(record: DNSCreate):
    """Create an A record in the Cloudflare zone.

    If content is empty, defaults to config.npm_host so callers can omit it
    when creating standard service records that should route through NPM.

    Architecture: most records should point to config.npm_host (NPM) so all
    traffic routes through the reverse proxy.  Use 192.0.2.1 for subdomains
    that exist only to support Cloudflare rules (no real origin server).
    """
    target_ip = record.content or config.npm_host
    if not target_ip:
        raise HTTPException(
            status_code=400,
            detail="No IP provided and NPM host could not be determined — check NPM URL in settings",
        )
    data = await cf_request(
        "POST",
        f"/zones/{config.cf_zone_id}/dns_records",
        json={
            "type": "A",
            "name": record.name,
            "content": target_ip,
            "proxied": record.proxied,
            "ttl": 1,  # ttl=1 means 'Auto' in Cloudflare
        },
    )
    result = data.get("result", {})
    logger.info("Created DNS record: %s → %s", result.get('name'), target_ip)
    emit_activity("dns.created", {"name": result.get("name"), "content": target_ip})
    return result


@app.delete("/api/dns/{record_id}", status_code=200)
async def delete_dns(record_id: str):
    """Delete a DNS A record by its Cloudflare record ID."""
    _validate_cf_id(record_id, "record_id")
    await cf_request("DELETE", f"/zones/{config.cf_zone_id}/dns_records/{record_id}")
    logger.info("Deleted DNS record: %s", record_id)
    emit_activity("dns.deleted", {"record_id": record_id})
    return {"success": True}


# ── Services ──────────────────────────────────────────────────────────────────


@app.get("/api/proxy-hosts")
async def list_proxy_hosts():
    """List all proxy hosts in NPM (full NPM state, not just console-created ones)."""
    return await npm_request("GET", "/api/nginx/proxy-hosts")


@app.get("/api/npm/certificates")
async def list_npm_certificates():
    """List certificates in NPM.

    Used by the Settings UI to help pick a wildcard certificate ID.
    """
    return await npm_request("GET", "/api/nginx/certificates")


@app.get("/api/npm/certificates/auto")
async def auto_detect_npm_wildcard_cert():
    """Auto-detect the wildcard certificate ID for *.DOMAIN in NPM."""
    if not config.domain:
        raise HTTPException(status_code=400, detail="Base domain not configured")

    wildcard = f"*.{config.domain}".lower()
    certs = await npm_request("GET", "/api/nginx/certificates")
    for c in certs:
        domains = [str(x).lower() for x in (c.get("domain_names") or [])]
        if wildcard in domains:
            return {
                "found": True,
                "cert_id": c.get("id"),
                "domain": wildcard,
                "certificate": c,
            }
    return {"found": False, "cert_id": None, "domain": wildcard}


@app.get("/api/scan")
async def scan():
    """Cross-reference Cloudflare DNS A records with NPM proxy hosts.

    Fetches both sources in parallel and returns a unified health picture.
    Works equally well on fresh deployments (all sections empty) and existing
    installations (shows all pre-existing services, flags any gaps).

    Response shape:
      services         NPM proxy hosts enriched with their DNS record.
                       status: 'ok' | 'missing_dns'

      unmatched_dns    A records pointing to config.npm_host with no proxy host.
                       Usually half-configured or orphaned (e.g. failed rollback).

      passthrough_dns  A records NOT pointing to config.npm_host — intentional
                       direct/CF-only targets like short domain → 192.0.2.1.

      npm_host         The NPM IP from config, so the frontend can use the correct
                       IP when creating DNS records without hardcoding it.
    """
    dns_records, dns_truncated = await _cf_fetch_zone_dns_a_records()
    npm_hosts = await npm_request("GET", "/api/nginx/proxy-hosts")

    # Domain → DNS record for O(1) cross-referencing
    dns_by_domain: dict[str, dict] = {r["name"]: r for r in dns_records}

    npm_domains: set[str] = set()
    services: list[dict] = []
    for host in npm_hosts:
        host_domains = _npm_host_domain_names(host)
        if not host_domains:
            continue
        domain = host_domains[0]
        npm_domains.update(host_domains)
        dns_record = dns_by_domain.get(domain)
        services.append(
            {
                "proxy_host": host,
                "dns_record": dns_record,
                "status": "ok" if dns_record else "missing_dns",
            }
        )

    # A records pointing to NPM with no proxy host — likely misconfigured/orphaned
    unmatched_dns: list[dict] = [
        r for r in dns_records if r["name"] not in npm_domains and r["content"] == config.npm_host
    ]

    # A records NOT pointing to NPM — intentional direct/CF-only targets
    passthrough_dns: list[dict] = [
        r for r in dns_records if r["name"] not in npm_domains and r["content"] != config.npm_host
    ]

    return {
        "services": services,
        "unmatched_dns": unmatched_dns,
        "passthrough_dns": passthrough_dns,
        "domain": config.domain,
        "npm_host": config.npm_host,  # for frontend use in "Add DNS" action
        "uptime_kuma_url": config.uptime_kuma_url,
        "homepage_url": config.homepage_url,
        "dockge_url": config.dockge_url,
        "wiki_url": config.wiki_url,
        "dns_truncated": dns_truncated,
    }


_reconcile_task: asyncio.Task[Any] | None = None


async def _reconcile_loop() -> None:
    """Periodic drift scan + optional webhook when issues exist."""
    interval = _env_int("RECONCILE_INTERVAL_SEC", 3600, 60)
    initial = _env_int("RECONCILE_INITIAL_DELAY_SEC", 120, 30)
    await asyncio.sleep(initial)
    while True:
        try:
            if config.is_configured():
                try:
                    scan_data = await scan()
                except Exception as e:
                    logger.warning("reconcile scan failed: %s", e)
                else:
                    ud = len(scan_data.get("unmatched_dns") or [])
                    md = sum(
                        1
                        for s in (scan_data.get("services") or [])
                        if s.get("status") == "missing_dns"
                    )
                    _metrics["reconcile_runs_total"] = (
                        int(_metrics.get("reconcile_runs_total", 0)) + 1
                    )
                    _metrics["reconcile_last_unix"] = int(time.time())
                    if ud or md:
                        emit_activity(
                            "reconcile.scan",
                            {"unmatched_dns": ud, "missing_dns_services": md},
                        )
                    hook = _reconcile_webhook_url()
                    if hook and (ud or md):
                        try:
                            async with httpx.AsyncClient(timeout=15.0) as wh:
                                await wh.post(
                                    hook,
                                    json={
                                        "event": "urler.reconcile",
                                        "unmatched_dns": ud,
                                        "missing_dns_services": md,
                                    },
                                )
                        except Exception as e:
                            logger.warning("reconcile webhook failed: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("reconcile loop error: %s", e)
        await asyncio.sleep(interval)


class ServiceCreate(BaseModel):
    subdomain: str
    forward_host: str
    forward_port: int
    forward_scheme: str = "http"
    websocket: bool = False
    ssl_verify_off: bool = False  # adds 'proxy_ssl_verify off;' for self-signed backends

    @field_validator("subdomain")
    @classmethod
    def validate_subdomain(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9\-]*$", v):
            raise ValueError("Subdomain must be lowercase alphanumeric and hyphens only")
        return v

    @field_validator("forward_host")
    @classmethod
    def validate_forward_host(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Forward host cannot be empty")
        return v

    @field_validator("forward_scheme")
    @classmethod
    def validate_scheme(cls, v: str) -> str:
        if v not in ("http", "https"):
            raise ValueError("Scheme must be http or https")
        return v

    @field_validator("forward_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v


class ServiceDelete(BaseModel):
    proxy_host_id: int
    domain: str
    dns_record_id: str | None = None

    @field_validator("proxy_host_id")
    @classmethod
    def validate_proxy_host_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("proxy_host_id must be > 0")
        return v

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("domain cannot be empty")
        return v


class ServiceEnabledUpdate(BaseModel):
    enabled: bool


class ServiceBatchRequest(BaseModel):
    items: list[ServiceCreate]
    dry_run: bool = True
    continue_on_error: bool = False

    @field_validator("items")
    @classmethod
    def validate_items_cap(cls, v: list[ServiceCreate]) -> list[ServiceCreate]:
        if not v:
            raise ValueError("No items provided")
        if len(v) > 100:
            raise ValueError("Batch import is limited to 100 items per request")
        return v


def _host_to_service_create(host: dict[str, Any], domain: str) -> ServiceCreate | None:
    """Rebuild ServiceCreate from NPM proxy host JSON for trash / restore."""
    dom = domain.strip().lower()
    suffix = "." + config.domain.strip().lower()
    if not dom.endswith(suffix):
        return None
    sub = dom[: -len(suffix)]
    if not sub or "." in sub:
        return None
    try:
        fs = str(host.get("forward_scheme") or "http")
        if fs not in ("http", "https"):
            fs = "http"
        fh = str(host.get("forward_host") or "").strip()
        fp = int(host.get("forward_port") or 0)
        if not fh or not (1 <= fp <= 65535):
            return None
        adv = str(host.get("advanced_config") or "")
        ssl_off = "proxy_ssl_verify off" in adv.replace(" ", "").lower()
        return ServiceCreate(
            subdomain=sub,
            forward_host=fh,
            forward_port=fp,
            forward_scheme=fs,
            websocket=bool(host.get("allow_websocket_upgrade")),
            ssl_verify_off=ssl_off,
        )
    except (ValueError, TypeError):
        return None


@app.post("/api/services", status_code=201)
async def create_service(svc: ServiceCreate):
    """Create a new service: Cloudflare DNS A record + NPM proxy host in one operation.

    DNS record:  {subdomain}.{domain}  A  {npm_host}  (routes to NPM)
    NPM host:    {subdomain}.{domain}  →  {scheme}://{host}:{port}

    Rollback: if NPM proxy host creation fails after DNS is created, the DNS
    record is deleted before re-raising.  If rollback also fails, the orphaned
    record ID is logged for manual cleanup in the Cloudflare dashboard.

    Retry: if a previous attempt left an orphaned DNS record, retrying will fail
    at Step 1 with 'record already exists'.  Delete the orphaned record from the
    DNS tab first, then retry here.
    """
    domain = f"{svc.subdomain}.{config.domain}"
    dns_record_id: str | None = None

    # Guard: npm_host is derived from npm_url; an unparseable URL returns "".
    if not config.npm_host:
        raise HTTPException(
            status_code=400,
            detail="Cannot determine NPM host IP from the configured NPM URL — check settings",
        )

    # Step 1 — DNS A record pointing to NPM, not the backend directly.
    # Traffic flows: browser → Cloudflare DNS → NPM → backend service.
    # NPM handles TLS termination and proxying; the backend never exposes a port.
    dns_data = await cf_request(
        "POST",
        f"/zones/{config.cf_zone_id}/dns_records",
        json={"type": "A", "name": domain, "content": config.npm_host, "proxied": False, "ttl": 1},
    )
    result = dns_data.get("result") or {}
    dns_record_id = result.get("id")
    if not dns_record_id:
        raise HTTPException(status_code=502, detail="Cloudflare returned success but no record ID")
    logger.info("DNS record created: %s → %s (id=%s)", domain, config.npm_host, dns_record_id)

    # Step 2 — NPM proxy host.  Roll back DNS on failure.
    advanced_config = "proxy_ssl_verify off;" if svc.ssl_verify_off else ""
    npm_payload = {
        "domain_names": [domain],
        "forward_scheme": svc.forward_scheme,
        "forward_host": svc.forward_host,
        "forward_port": svc.forward_port,
        "allow_websocket_upgrade": svc.websocket,
        "block_exploits": True,
        "access_list_id": 0,
        "certificate_id": config.npm_cert_id,
        "ssl_forced": True,
        "http2_support": False,
        "meta": {},
        "locations": [],
        "advanced_config": advanced_config,
    }
    try:
        await npm_request("POST", "/api/nginx/proxy-hosts", json=npm_payload)
    except HTTPException:
        logger.warning(
            "NPM host creation failed for %s; rolling back DNS %s", domain, dns_record_id
        )
        try:
            await cf_request("DELETE", f"/zones/{config.cf_zone_id}/dns_records/{dns_record_id}")
            logger.info("DNS rollback succeeded: %s", domain)
        except Exception as rollback_err:
            logger.error(
                "DNS rollback FAILED for %s (id=%s): %s — manual cleanup required in Cloudflare dashboard",
                domain,
                dns_record_id,
                rollback_err,
            )
        raise  # re-raise original NPM error to the frontend

    logger.info(
        "Service created: %s → %s://%s:%s",
        domain,
        svc.forward_scheme,
        svc.forward_host,
        svc.forward_port,
    )
    emit_activity(
        "service.created",
        {
            "domain": domain,
            "backend": f"{svc.forward_scheme}://{svc.forward_host}:{svc.forward_port}",
        },
    )
    return {
        "success": True,
        "domain": domain,
        "dns_id": dns_record_id,
        "backend": f"{svc.forward_scheme}://{svc.forward_host}:{svc.forward_port}",
    }


@app.post("/api/services/batch", status_code=200)
async def batch_services(payload: ServiceBatchRequest):
    """Batch create services with optional dry-run preview."""
    if not payload.items:
        raise HTTPException(status_code=400, detail="No items provided")

    # Gather existing domains for conflict warnings in dry-run.
    (dns_list, _), npm_hosts = await asyncio.gather(
        _cf_fetch_zone_dns_a_records(),
        npm_request("GET", "/api/nginx/proxy-hosts"),
    )
    existing_dns = {r.get("name") for r in dns_list}
    existing_npm: set[str] = set()
    for h in npm_hosts:
        existing_npm.update(_npm_host_domain_names(h))

    plan: list[dict[str, Any]] = []
    for item in payload.items:
        domain = f"{item.subdomain}.{config.domain}"
        warnings: list[str] = []
        if domain in existing_dns:
            warnings.append("DNS record already exists")
        if domain in existing_npm:
            warnings.append("NPM proxy host already exists")
        plan.append(
            {
                "domain": domain,
                "backend": f"{item.forward_scheme}://{item.forward_host}:{item.forward_port}",
                "warnings": warnings,
            }
        )

    if payload.dry_run:
        return {"success": True, "dry_run": True, "plan": plan}

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in payload.items:
        try:
            res = await create_service(item)
            created.append(res)
        except HTTPException as e:
            failed.append(
                {
                    "domain": f"{item.subdomain}.{config.domain}",
                    "error": str(e.detail),
                    "status_code": e.status_code,
                }
            )
            if not payload.continue_on_error:
                break
        except Exception as e:
            failed.append(
                {
                    "domain": f"{item.subdomain}.{config.domain}",
                    "error": str(e),
                    "status_code": 500,
                }
            )
            if not payload.continue_on_error:
                break

    emit_activity(
        "service.batch_apply",
        {"created": len(created), "failed": len(failed), "items": len(payload.items)},
    )
    return {
        "success": len(failed) == 0,
        "dry_run": False,
        "created": created,
        "failed": failed,
    }


@app.post("/api/services/delete", status_code=200)
async def delete_service(payload: ServiceDelete):
    """Delete service in one action: NPM proxy host + matching Cloudflare DNS A record."""
    npm_deleted = False
    dns_deleted = False
    dns_error: str | None = None
    dns_target_id = payload.dns_record_id

    restore_svc: ServiceCreate | None = None
    try:
        raw_h = await npm_request("GET", f"/api/nginx/proxy-hosts/{payload.proxy_host_id}")
        host_dict = _normalize_npm_host_payload(raw_h)
        if host_dict:
            restore_svc = _host_to_service_create(host_dict, payload.domain)
    except Exception as e:
        logger.warning("Could not snapshot NPM host for trash/restore: %s", e)

    # Step 1: delete proxy host
    await npm_request("DELETE", f"/api/nginx/proxy-hosts/{payload.proxy_host_id}")
    npm_deleted = True

    # Step 2: delete DNS record (by explicit ID or best-effort lookup by domain+npm_host)
    if dns_target_id:
        _validate_cf_id(dns_target_id, "dns_record_id")
    else:
        cf_data = await cf_request(
            "GET",
            f"/zones/{config.cf_zone_id}/dns_records",
            params={"name": payload.domain, "type": "A", "per_page": 10},
        )
        for rec in cf_data.get("result", []):
            if rec.get("name") == payload.domain and rec.get("content") == config.npm_host:
                dns_target_id = rec.get("id")
                break

    if dns_target_id:
        try:
            await cf_request("DELETE", f"/zones/{config.cf_zone_id}/dns_records/{dns_target_id}")
            dns_deleted = True
        except HTTPException as e:
            dns_error = str(e.detail)
            logger.warning(
                "DNS delete failed after NPM delete: domain=%s, dns_record_id=%s, detail=%s",
                payload.domain,
                dns_target_id,
                dns_error,
            )

    if restore_svc:
        trash_prepend_service_entry(
            {
                "id": secrets.token_urlsafe(12),
                "ts": datetime.now(UTC).isoformat(),
                "domain": payload.domain,
                "service": restore_svc.model_dump(),
            }
        )

    logger.info(
        "Deleted service: domain=%s, proxy_host_id=%s, npm_deleted=%s, dns_deleted=%s",
        payload.domain,
        payload.proxy_host_id,
        npm_deleted,
        dns_deleted,
    )
    emit_activity(
        "service.deleted",
        {
            "domain": payload.domain,
            "proxy_host_id": payload.proxy_host_id,
            "dns_deleted": dns_deleted,
            "restorable": restore_svc is not None,
        },
    )
    return {
        "success": True,
        "proxy_deleted": npm_deleted,
        "dns_deleted": dns_deleted,
        "dns_error": dns_error,
    }


# Fields NPM accepts on PUT — same set as create_service POST payload, plus enabled.
_SAFE_NPM_PUT_KEYS: frozenset[str] = frozenset(
    {
        "domain_names",
        "forward_scheme",
        "forward_host",
        "forward_port",
        "certificate_id",
        "ssl_forced",
        "http2_support",
        "block_exploits",
        "access_list_id",
        "advanced_config",
        "meta",
        "locations",
        "allow_websocket_upgrade",
        "enabled",
    }
)

_NPM_PUT_DEFAULTS: dict[str, Any] = {
    "block_exploits": True,
    "access_list_id": 0,
    "meta": {},
    "locations": [],
    "http2_support": False,
    "ssl_forced": True,
    "allow_websocket_upgrade": False,
    "advanced_config": "",
}


@app.post("/api/services/{proxy_host_id}/enabled", status_code=200)
async def set_service_enabled(proxy_host_id: int, payload: ServiceEnabledUpdate):
    """Enable/disable an existing NPM proxy host."""
    if proxy_host_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid proxy_host_id")

    raw = await npm_request("GET", f"/api/nginx/proxy-hosts/{proxy_host_id}")
    host = _normalize_npm_host_payload(raw)
    if not host:
        raise HTTPException(status_code=404, detail="Proxy host not found")

    update_body: dict[str, Any] = {k: v for k, v in host.items() if k in _SAFE_NPM_PUT_KEYS}
    for key, default in _NPM_PUT_DEFAULTS.items():
        if key in _SAFE_NPM_PUT_KEYS and key not in update_body:
            update_body[key] = default
    update_body["enabled"] = payload.enabled

    result = await npm_request("PUT", f"/api/nginx/proxy-hosts/{proxy_host_id}", json=update_body)
    logger.info("Set proxy host %s enabled=%s", proxy_host_id, payload.enabled)
    emit_activity(
        "service.proxy_enabled",
        {"proxy_host_id": proxy_host_id, "enabled": payload.enabled},
    )
    return {"success": True, "proxy_host": result}


@app.get("/api/activity")
async def get_activity(limit: int = 200):
    """Recent operator actions (JSONL-backed)."""
    limit = min(max(1, limit), 500)
    path = activity_log_path()
    if not path.exists():
        return {"events": []}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"events": []}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    picked = lines[-limit:]
    events: list[dict[str, Any]] = []
    for ln in reversed(picked):
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return {"events": events}


@app.get("/api/integrations/health")
async def integrations_health():
    """Live Cloudflare + NPM reachability (short TTL cache)."""
    global _health_cache, _health_cache_mono
    now = time.monotonic()
    if _health_cache is not None and (now - _health_cache_mono) < _HEALTH_TTL_SEC:
        return _health_cache

    async def _check_cf() -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            await cf_request(
                "GET", f"/zones/{config.cf_zone_id}/dns_records", params={"per_page": 1}
            )
            return {"ok": True, "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except HTTPException as e:
            return {
                "ok": False,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(e.detail),
            }
        except Exception as e:
            return {"ok": False, "latency_ms": None, "error": str(e)}

    async def _check_npm() -> dict[str, Any]:
        t1 = time.perf_counter()
        try:
            await npm_request("GET", "/api/nginx/settings")
            return {"ok": True, "latency_ms": int((time.perf_counter() - t1) * 1000)}
        except HTTPException as e:
            return {
                "ok": False,
                "latency_ms": int((time.perf_counter() - t1) * 1000),
                "error": str(e.detail),
            }
        except Exception as e:
            return {"ok": False, "latency_ms": None, "error": str(e)}

    cf_status, npm_status = await asyncio.gather(_check_cf(), _check_npm())
    out: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "cloudflare": cf_status,
        "npm": npm_status,
    }

    _health_cache = out
    _health_cache_mono = now
    return out


@app.get("/api/metrics")
async def get_metrics():
    """Lightweight counters for monitoring (in-memory; reset on container restart).

    Exposed in the console header next to integration health. Safe to scrape
    periodically; values are not persisted to disk.
    """
    out = dict(_metrics)
    last = out.get("reconcile_last_unix")
    if isinstance(last, int):
        out["reconcile_last_iso"] = datetime.fromtimestamp(last, UTC).isoformat()
    return out


@app.get("/api/config/export")
async def export_config_backup():
    """Download masked configuration as JSON (no raw secrets)."""
    snap = await get_config()
    body = {
        "exported_at": datetime.now(UTC).isoformat(),
        "urler_version": __version__,
        "config": snap,
        "note": "Secrets are masked. For a full credential backup, securely copy /data/config.json from the host.",
    }
    raw = json.dumps(body, indent=2, ensure_ascii=False)
    return Response(
        content=raw,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="urler-export.json"'},
    )


@app.get("/api/trash")
async def list_service_trash():
    """Recently deleted services that can be recreated from a snapshot."""
    return {"items": _trash_load()}


_TRASH_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@app.post("/api/trash/{entry_id}/restore", status_code=201)
async def restore_trashed_service(entry_id: str):
    if not _TRASH_ENTRY_ID.match(entry_id):
        raise HTTPException(status_code=400, detail="Invalid trash entry id")
    rows = _trash_load()
    entry = next((r for r in rows if r.get("id") == entry_id), None)
    if not entry or "service" not in entry:
        raise HTTPException(status_code=404, detail="Trash entry not found")
    try:
        svc = ServiceCreate(**entry["service"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid trash payload: {e}") from e
    await create_service(svc)
    trash_remove(entry_id)
    emit_activity("service.restored_from_trash", {"domain": entry.get("domain")})
    return {"success": True}


# ── Frontend (catch-all) ──────────────────────────────────────────────────────
# /static/* is handled by the StaticFiles mount.
# Everything else (including bare /) returns the React SPA — required for
# browser history navigation (e.g. reloading a tab).
# All /api/* routes defined above take precedence over this catch-all.

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """Serve the React SPA for all non-API, non-static routes."""
    return FileResponse("static/index.html")
