"""
PenguinNest Console — API Backend
==================================
FastAPI application serving the unified PenguinNest management console at
your internal URL (e.g. console.yourdomain.com — not publicly exposed).

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
  CONFIG_FILE     Override path to config JSON           (/data/config.json)

Manages three resource types:
  Links      Cloudflare Bulk Redirect list items (SHORT_DOMAIN/*)
  DNS        Cloudflare A records for *.DOMAIN
  Services   DNS record + NPM proxy host in one operation
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

logger = logging.getLogger("console")

CF_BASE     = "https://api.cloudflare.com/client/v4"
MASK_CHAR   = "•"   # used to mask secrets in GET /api/config responses
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    """Runtime configuration.  Populated by _load_config() from file + env."""

    cf_api_token:  str = ""
    cf_account_id: str = ""
    cf_zone_id:    str = ""
    npm_url:       str = ""
    npm_email:     str = ""
    npm_password:  str = ""
    npm_cert_id:   int = 2
    domain:        str = ""
    short_domain:  str = ""
    cf_list_name:  str = "shortlinks"

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
            "Content-Type":  "application/json",
        }

    # ── State helpers ─────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True when all required fields are populated."""
        return bool(
            self.cf_api_token and self.cf_account_id and self.cf_zone_id
            and self.npm_email and self.npm_password
            and self.domain and self.npm_url
        )

    def missing_fields(self) -> list[str]:
        """Names of required fields that are currently empty."""
        required = {
            "cf_api_token":  self.cf_api_token,
            "cf_account_id": self.cf_account_id,
            "cf_zone_id":    self.cf_zone_id,
            "npm_url":       self.npm_url,
            "npm_email":     self.npm_email,
            "npm_password":  self.npm_password,
            "domain":        self.domain,
        }
        return [k for k, v in required.items() if not v]


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
_npm_token:         str | None = None
_npm_token_expires: float      = 0.0


# ── Config loading ─────────────────────────────────────────────────────────────

def _load_config() -> None:
    """Populate the global config from /data/config.json, then env vars, then defaults.

    Priority (highest wins): config file > environment variable > built-in default.
    Called once at startup and again after the wizard saves new settings.
    Clears all API caches so stale tokens/IDs from previous credentials are dropped.
    """
    global config, _list_id_cache, _npm_token, _npm_token_expires

    # Reset caches — credentials may have changed
    _list_id_cache    = None
    _npm_token        = None
    _npm_token_expires = 0.0

    file_data: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            file_data = json.loads(CONFIG_FILE.read_text())
            logger.info(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Could not read config file {CONFIG_FILE}: {e}")

    def _get(key: str, default: str = "") -> str:
        """Read key from config file, then env var (UPPER_CASE), then default."""
        return str(file_data.get(key) or os.environ.get(key.upper(), default) or default)

    # Parse npm_cert_id separately because it needs an int conversion.
    try:
        raw_cert = int(file_data.get("npm_cert_id") or os.environ.get("NPM_CERT_ID", "2") or "2")
        npm_cert_id = max(raw_cert, 1)   # silently clamp — 0 = no cert, not supported
        if raw_cert < 1:
            logger.warning(f"NPM_CERT_ID was {raw_cert}; clamped to 1")
    except (ValueError, TypeError):
        logger.warning("NPM_CERT_ID is not a valid integer; using default 2")
        npm_cert_id = 2

    config = Config(
        cf_api_token  = _get("cf_api_token"),
        cf_account_id = _get("cf_account_id"),
        cf_zone_id    = _get("cf_zone_id"),
        npm_url       = _get("npm_url",      ""),
        npm_email     = _get("npm_email"),
        npm_password  = _get("npm_password"),
        npm_cert_id   = npm_cert_id,
        domain        = _get("domain",       ""),
        short_domain  = _get("short_domain", ""),
        cf_list_name  = _get("cf_list_name", "shortlinks"),
    )


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
    _load_config()
    logger.info("PenguinNest Console starting")
    if config.is_configured():
        logger.info(f"  Domain:       {config.domain}")
        logger.info(f"  Short domain: {config.short_domain}")
        logger.info(f"  CF list:      {config.cf_list_name}")
        logger.info(f"  NPM:          {config.npm_url}")
        try:
            list_id = await get_list_id()
            if list_id:
                logger.info(f"  CF list ID:   {list_id} (found)")
            else:
                logger.warning(f"  CF list '{config.cf_list_name}' not found — Tofu bootstrap required")
        except Exception as e:
            logger.warning(f"  Cloudflare connectivity check failed: {e}")
    else:
        logger.warning(f"  Not configured — missing: {', '.join(config.missing_fields())}")
        logger.warning("  Open http://localhost:8000 in your browser to complete setup.")
    yield
    logger.info("PenguinNest Console shutting down")


app = FastAPI(title="PenguinNest Console", lifespan=lifespan)


# ── Middleware ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def require_configured(request: Request, call_next):
    """Block functional API endpoints when the app has not been configured yet.

    Always allows:
      /api/health          — Docker healthcheck must work before setup
      /api/config*         — wizard reads and writes config
      Everything non-/api  — static files and the SPA itself
    """
    path = request.url.path
    if (
        not path.startswith("/api/")
        or path == "/api/health"
        or path.startswith("/api/config")
    ):
        return await call_next(request)

    if not config.is_configured():
        return JSONResponse(
            status_code=503,
            content={"detail": "Application not configured. Complete the setup wizard first."},
        )
    return await call_next(request)


# ── API helpers ───────────────────────────────────────────────────────────────

async def cf_request(method: str, path: str, **kwargs) -> Any:
    """Make an authenticated request to the Cloudflare API.

    Uses credentials from the current global config.  Wraps httpx errors into
    HTTPException so the actual Cloudflare error message reaches the frontend.
    Timeout is explicit at 10s; httpx default is 5s but Cloudflare can be slow
    on write operations.
    """
    url = f"{CF_BASE}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.request(method, url, headers=config.cf_headers, **kwargs)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            try:
                body   = e.response.json()
                errors = body.get("errors", [])
                msg    = errors[0]["message"] if errors else e.response.text
            except Exception:
                msg = e.response.text
            logger.error(f"Cloudflare {method} {path} → {e.response.status_code}: {msg}")
            raise HTTPException(status_code=e.response.status_code, detail=f"Cloudflare: {msg}")
        except httpx.RequestError as e:
            logger.error(f"Cloudflare unreachable: {e}")
            raise HTTPException(status_code=503, detail=f"Cannot reach Cloudflare: {str(e)}")


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
            logger.info(f"Cached CF list ID: {_list_id_cache}")
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

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(
                f"{config.npm_url.rstrip('/')}/api/tokens",
                json={"identity": config.npm_email, "secret": config.npm_password},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"NPM authentication failed: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Cannot reach NPM at {config.npm_url}: {str(e)}")
        # Response body is buffered; r.json() is safe to call inside the block.
        token = r.json().get("token")
        if not token:
            raise HTTPException(status_code=502, detail="NPM returned success but no token in response")
        _npm_token         = token
        _npm_token_expires = now + 3600

    logger.info("NPM token refreshed")
    return token  # local var; _npm_token (global) is also set for cache use


async def npm_request(method: str, path: str, **kwargs) -> Any:
    """Make an authenticated request to the Nginx Proxy Manager API.

    Acquires a token on first call (or after expiry) and wraps errors into
    HTTPException with the actual NPM error message.
    """
    token = await get_npm_token()
    url   = f"{config.npm_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.request(
                method, url,
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
            logger.error(f"NPM {method} {path} → {e.response.status_code}: {msg}")
            raise HTTPException(status_code=e.response.status_code, detail=f"NPM: {msg}")
        except httpx.RequestError as e:
            logger.error(f"NPM unreachable: {e}")
            raise HTTPException(status_code=503, detail=f"Cannot reach NPM at {config.npm_url}: {str(e)}")


# ── Input validation helpers ───────────────────────────────────────────────────

_CF_ID_RE = re.compile(r'^[a-f0-9]{1,64}$')


def _validate_cf_id(id_value: str, label: str) -> None:
    """Reject Cloudflare record/item IDs that are not hex strings.

    Cloudflare IDs are 32-character hex strings (e.g. DNS record IDs, list
    item IDs).  Validating them before inserting into URL paths prevents
    path-traversal payloads like '../../users/tokens' from reaching the
    Cloudflare API.
    """
    if not _CF_ID_RE.match(id_value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: must be a hex string")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Health check. Used by Docker healthcheck and Uptime Kuma.

    Always returns 200 — even when unconfigured — so the container is considered
    healthy while the setup wizard is being completed.
    """
    return {
        "status":     "ok",
        "service":    "console",
        "version":    "1.0.0",
        "configured": config.is_configured(),
    }


# ── Configuration endpoints ───────────────────────────────────────────────────

@app.get("/api/config/status")
async def config_status():
    """Return whether the app is configured.

    Called by the frontend on load to decide whether to show the wizard or the
    main app.  Always accessible regardless of configuration state.
    """
    return {
        "configured":     config.is_configured(),
        "missing_fields": config.missing_fields(),
    }


@app.get("/api/config")
async def get_config():
    """Return current configuration with sensitive fields masked.

    Used by the settings form to pre-populate fields.  Tokens and passwords
    are replaced with a masked representation (first+last 4 chars visible).
    To change a sensitive field, clear it and enter the new value; the server
    detects unchanged masked values via _resolve() and keeps the existing credential.
    Always accessible regardless of configuration state.
    """
    return {
        "configured":     config.is_configured(),
        "missing_fields": config.missing_fields(),
        "cf_api_token":   _mask(config.cf_api_token),
        "cf_account_id":  config.cf_account_id,
        "cf_zone_id":     config.cf_zone_id,
        "npm_url":        config.npm_url,
        "npm_email":      config.npm_email,
        "npm_password":   _mask(config.npm_password),
        "npm_cert_id":    config.npm_cert_id,
        "domain":         config.domain,
        "short_domain":   config.short_domain,
        "cf_list_name":   config.cf_list_name,
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

    cf_api_token:  str
    cf_account_id: str
    cf_zone_id:    str
    npm_url:       str = ""
    npm_email:     str
    npm_password:  str
    npm_cert_id:   int = 2
    domain:        str = ""
    short_domain:  str = ""
    cf_list_name:  str = "shortlinks"

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
            return v  # empty allowed; is_configured() enforces the required check
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("NPM URL must start with http:// or https://")
        return v


@app.post("/api/config/test")
async def test_config(proposal: ConfigProposal):
    """Test Cloudflare and NPM connectivity using the provided credentials.

    Performs a live check against both services without saving anything.
    Returns per-service results so the wizard can give specific feedback.
    Always accessible regardless of configuration state.
    """
    # Resolve masked fields against current config.  When called from the settings
    # modal, the form contains masked display values from GET /api/config.
    # _resolve() detects those and substitutes the real stored credentials so the
    # test uses values that actually work, not bullet-char placeholders.
    cf_token = _resolve(proposal.cf_api_token, config.cf_api_token)
    npm_pass  = _resolve(proposal.npm_password,  config.npm_password)

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
                body   = e.response.json()
                errors = body.get("errors", [])
                msg    = errors[0]["message"] if errors else f"HTTP {e.response.status_code}"
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
            return {"ok": False, "error": f"Authentication failed (HTTP {e.response.status_code}) — check email/password"}
        except httpx.RequestError as e:
            return {"ok": False, "error": f"Connection failed — check NPM URL ({e})"}

    cf_result, npm_result = await asyncio.gather(_check_cf(), _check_npm())

    return {
        "cloudflare": cf_result,
        "npm":        npm_result,
        "all_ok":     cf_result["ok"] and npm_result["ok"],
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
    config_data = {
        "cf_api_token":  _resolve(proposal.cf_api_token,  config.cf_api_token),
        "cf_account_id": proposal.cf_account_id,
        "cf_zone_id":    proposal.cf_zone_id,
        "npm_url":       proposal.npm_url,
        "npm_email":     proposal.npm_email,
        "npm_password":  _resolve(proposal.npm_password, config.npm_password),
        "npm_cert_id":   proposal.npm_cert_id,
        "domain":        proposal.domain,
        "short_domain":  proposal.short_domain,
        "cf_list_name":  proposal.cf_list_name,
    }

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_FILE.write_text(json.dumps(config_data, indent=2))
        CONFIG_FILE.chmod(0o600)  # credentials — owner read/write only
    except OSError as e:
        raise HTTPException(
            500,
            f"Could not write config to {CONFIG_FILE}: {e}. "
            "Ensure the /data volume is mounted (see compose.yaml).",
        )

    _load_config()
    logger.info("Configuration saved and reloaded")
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
    data = await cf_request(
        "GET",
        f"/accounts/{config.cf_account_id}/rules/lists/{list_id}/items",
        params={"per_page": 500},
    )
    return {"items": data.get("result", []), "list_exists": True}


class LinkCreate(BaseModel):
    slug:   str
    target: str

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


@app.post("/api/links", status_code=201)
async def create_link(link: LinkCreate):
    """Add a redirect entry: SHORT_DOMAIN/{slug} → target (301).

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
        json=[{"redirect": {"source_url": source_url, "target_url": link.target, "status_code": 301}}],
    )
    logger.info(f"Created short link: {source_url} → {link.target}")
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
    logger.info(f"Deleted short link: {item_id}")
    return {"success": True}


# ── DNS Records ───────────────────────────────────────────────────────────────

@app.get("/api/dns")
async def list_dns():
    """List all A records in the zone, ordered by name.

    Only A records are returned — this console manages services via A records.
    Capped at 100 records (well above any realistic home zone size).
    """
    data = await cf_request(
        "GET",
        f"/zones/{config.cf_zone_id}/dns_records",
        params={"per_page": 100, "order": "name", "type": "A"},
    )
    return data.get("result", [])


class DNSCreate(BaseModel):
    # Accepts bare subdomain ('myservice') or FQDN — Cloudflare normalises both.
    name:    str
    content: str  = ""    # default resolved to config.npm_host at request time
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
            return v   # empty string handled at endpoint level (defaults to npm_host)
        try:
            ipaddress.IPv4Address(v.strip())
        except ValueError:
            raise ValueError("Content must be a valid IPv4 address")
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
            "type":    "A",
            "name":    record.name,
            "content": target_ip,
            "proxied": record.proxied,
            "ttl":     1,   # ttl=1 means 'Auto' in Cloudflare
        },
    )
    result = data.get("result", {})
    logger.info(f"Created DNS record: {result.get('name')} → {target_ip}")
    return result


@app.delete("/api/dns/{record_id}", status_code=200)
async def delete_dns(record_id: str):
    """Delete a DNS A record by its Cloudflare record ID."""
    _validate_cf_id(record_id, "record_id")
    await cf_request("DELETE", f"/zones/{config.cf_zone_id}/dns_records/{record_id}")
    logger.info(f"Deleted DNS record: {record_id}")
    return {"success": True}


# ── Services ──────────────────────────────────────────────────────────────────

@app.get("/api/proxy-hosts")
async def list_proxy_hosts():
    """List all proxy hosts in NPM (full NPM state, not just console-created ones)."""
    return await npm_request("GET", "/api/nginx/proxy-hosts")


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
    cf_data, npm_hosts = await asyncio.gather(
        cf_request(
            "GET",
            f"/zones/{config.cf_zone_id}/dns_records",
            params={"per_page": 100, "order": "name", "type": "A"},
        ),
        npm_request("GET", "/api/nginx/proxy-hosts"),
    )

    dns_records: list[dict] = cf_data.get("result", [])

    # Domain → DNS record for O(1) cross-referencing
    dns_by_domain: dict[str, dict] = {r["name"]: r for r in dns_records}

    npm_domains: set[str]  = set()
    services:    list[dict] = []
    for host in npm_hosts:
        domain = (host.get("domain_names") or [None])[0]
        if not domain:
            continue
        npm_domains.add(domain)
        dns_record = dns_by_domain.get(domain)
        services.append({
            "proxy_host": host,
            "dns_record": dns_record,
            "status":     "ok" if dns_record else "missing_dns",
        })

    # A records pointing to NPM with no proxy host — likely misconfigured/orphaned
    unmatched_dns: list[dict] = [
        r for r in dns_records
        if r["name"] not in npm_domains and r["content"] == config.npm_host
    ]

    # A records NOT pointing to NPM — intentional direct/CF-only targets
    passthrough_dns: list[dict] = [
        r for r in dns_records
        if r["name"] not in npm_domains and r["content"] != config.npm_host
    ]

    return {
        "services":        services,
        "unmatched_dns":   unmatched_dns,
        "passthrough_dns": passthrough_dns,
        "npm_host":        config.npm_host,   # for frontend use in "Add DNS" action
    }


class ServiceCreate(BaseModel):
    subdomain:      str
    forward_host:   str
    forward_port:   int
    forward_scheme: str  = "http"
    websocket:      bool = False
    ssl_verify_off: bool = False   # adds 'proxy_ssl_verify off;' for self-signed backends

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
    logger.info(f"DNS record created: {domain} → {config.npm_host} (id={dns_record_id})")

    # Step 2 — NPM proxy host.  Roll back DNS on failure.
    advanced_config = "proxy_ssl_verify off;" if svc.ssl_verify_off else ""
    npm_payload = {
        "domain_names":            [domain],
        "forward_scheme":          svc.forward_scheme,
        "forward_host":            svc.forward_host,
        "forward_port":            svc.forward_port,
        "allow_websocket_upgrade": svc.websocket,
        "block_exploits":          True,
        "access_list_id":          0,
        "certificate_id":          config.npm_cert_id,
        "ssl_forced":              True,
        "http2_support":           False,
        "meta":                    {},
        "locations":               [],
        "advanced_config":         advanced_config,
    }
    try:
        await npm_request("POST", "/api/nginx/proxy-hosts", json=npm_payload)
    except HTTPException:
        logger.warning(f"NPM host creation failed for {domain}; rolling back DNS {dns_record_id}")
        try:
            await cf_request("DELETE", f"/zones/{config.cf_zone_id}/dns_records/{dns_record_id}")
            logger.info(f"DNS rollback succeeded: {domain}")
        except Exception as rollback_err:
            logger.error(
                f"DNS rollback FAILED for {domain} (id={dns_record_id}): {rollback_err} "
                "— manual cleanup required in Cloudflare dashboard"
            )
        raise   # re-raise original NPM error to the frontend

    logger.info(f"Service created: {domain} → {svc.forward_scheme}://{svc.forward_host}:{svc.forward_port}")
    return {
        "success": True,
        "domain":  domain,
        "dns_id":  dns_record_id,
        "backend": f"{svc.forward_scheme}://{svc.forward_host}:{svc.forward_port}",
    }


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
