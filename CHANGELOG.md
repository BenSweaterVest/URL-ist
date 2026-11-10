# Changelog

Notable changes to URLer. Versions follow `__version__` in `main.py`.

## 1.4.0

Security

- Starlette 0.49.1 had five published advisories (and failed the CI `pip-audit` step). Upgraded to FastAPI 0.162.0 and Starlette 1.10.0, with uvicorn 0.54.0 and pydantic 2.14.2.
- Changing the console password signs out every other browser. Sessions carry a fingerprint of the password; the browser that made the change stays signed in.
- `GET /api/config` no longer reveals the first and last four characters of secrets. Passwords show a fixed mask and long tokens only their last four characters.
- Responses forbid framing and set `nosniff` and `Referrer-Policy: no-referrer`; API responses are not cached.

Fixes

- The session key written by the setup wizard is used from the next request. The "restart the container once" step, banner and toast are gone; `session_secret_restart_needed` is always `false`.
- NPM requests re-authenticate once when NPM rejects a cached token instead of failing until the hourly refresh. NPM error bodies (`{"error": {"message": ...}}`) are shown as readable text.
- Retrying a service whose earlier attempt left a DNS record behind now works: an A record already pointing at NPM is reused. A record pointing elsewhere, or an existing NPM host, is a 409 before anything is written. Rollback never deletes a record the call did not create.
- Restoring a service from the trash keeps its NPM settings (locations, advanced config, HSTS, HTTP/2, caching, access list, certificate). Older trash entries still restore with defaults.
- Short-link preflight accepts a proxied A, AAAA or CNAME record for the short domain.
- Editing a short link without changes no longer deletes and recreates the Cloudflare list item.
- Batch import flags subdomains listed twice and applies only the first.
- DNS record names are validated (hostname labels, optional `*.` prefix, or `@`).
- Tofu `short_subdomain` validation matches the DNS label rule.

Added

- Service create returns `proxy_host_id` and `dns_reused`.
- Short-link delete events in the Activity log include the slug.
- The SPA returns to the login screen when a session expires, remembers the active tab in the URL, uses real buttons for tabs, and warns about a duplicate slug before calling Cloudflare.

## 1.3.0

Security

- A console password now gates every API route on its own, independent of whether the rest of the configuration is complete. Previously an invalid setting such as an NPM cert ID of 0 reopened the wizard endpoints to anyone on the network, including the ability to set a new password.
- An install that is configured but has no console password (env-only deployment without `CONSOLE_PASSWORD`) now stays in setup mode: only the wizard endpoints respond until a password is chosen. Before, every read endpoint was open and every write was blocked by CSRF with no way to fix it.
- Unknown `/api/*` paths return a JSON 404 instead of the SPA page.

Fixes

- `session_secret_source` reports the key the running process is using. It used to re-read disk and claim `file` right after the wizard saved a key the middleware had never loaded, which hid the "restart once" reminder. A new `session_secret_restart_needed` flag drives that reminder.
- Activity log trimming is a single pass over the file. At the 2 MB threshold the old loop blocked the server for around 15 seconds.
- Short link create, edit and delete wait for Cloudflare's bulk list operation to finish and surface its error (for example a duplicate slug). Responses carry `pending: true` if Cloudflare is still applying the change after ten seconds.
- Short Links Domain, Base Domain and Cloudflare IDs are normalised on save. A short domain pasted with `https://` no longer produces `https://https://...` source URLs.
- List items with no explicit status code are treated as 301, matching Cloudflare, instead of 302. Editing such an item keeps it a 301.
- Enable/disable uses NPM's `/enable` and `/disable` endpoints rather than rebuilding the host for a PUT. A host already in the requested state is treated as success.
- Subdomains are validated as a DNS label (1 to 63 characters, no leading or trailing hyphen).
- The login rate limiter forgets addresses whose window has passed instead of growing for the life of the process.
- Two toasts raised in the same millisecond no longer share a key.

Added

- `dns_target_ip` setting (`DNS_TARGET_IP` env). Service A records point here; blank means the host in the NPM URL. Required when the NPM URL uses a hostname, since Cloudflare only accepts an IPv4 address as A record content. `GET /api/scan` exposes the effective value as `dns_target` (was `npm_host`).
- The wizard reopens prefilled from saved settings when a required value goes missing later. Masked secrets are kept unless replaced.
- `GET /api/dns` includes `npm_proxy_domains_available`; the DNS tab warns when NPM is unreachable and the proxy-host delete guardrail cannot work.
- README notes on cookie revocation, setup mode, and asynchronous Cloudflare writes.
- The wizard no longer shows the cert auto-detect button, which needs a fully configured app; it stays in Settings. Steps in the service-created checklist are numbered without gaps.

## 1.2.0

Integration URL validation, redirect status codes on create and edit, reconcile loop, activity log, metrics, request tracing, config snapshots, session hardening. See git history for details.
