# Changelog

Notable changes to URLer. Versions follow `__version__` in `main.py`.

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
