# URLer

A self-hosted management console for [Cloudflare](https://cloudflare.com) + [Nginx Proxy Manager](https://nginxproxymanager.com), built for homelab use.

**Four panels, one app:**

| Panel | What it does |
|-------|-------------|
| **Short Links** | Create, edit, and delete `short.yourdomain.com/slug → destination` redirects via Cloudflare Bulk Redirects (302 by default so edits work after a browser has visited the slug) |
| **DNS** | View, add, and delete Cloudflare A records for your zone |
| **Services** | Create services (DNS + NPM proxy host), delete them together, enable/disable proxy hosts, restore from trash, or batch-import from JSON with dry-run preview |
| **Activity** | Recent operator actions (links, DNS, services, reconcile) with expandable JSON detail |

### What each panel does

- **Short Links** — Add slugs (choose redirect status 301/302/307/308 on create; default 302), change destinations in place, delete entries. Preflight checks list + DNS readiness. Requires one-time Tofu bootstrap.
- **DNS** — Manage A records (full zone list via API pagination; table pages locally when there are many rows). Warns before deleting the short-links domain record or any name that still has an NPM proxy host (use Services to remove both). If NPM cannot be reached the tab says so, because that second warning depends on NPM's host list.
- **Services** — One form creates `subdomain.domain` DNS (→ NPM) and an NPM proxy host (→ your backend). Delete removes NPM host and DNS (with warnings on partial failure); recently deleted entries can be **restored** from a trash snapshot. Toggle enables/disables the NPM proxy without deleting DNS. **Batch Import** accepts a JSON array (`subdomain`, `forward_host`, `forward_port`, optional `forward_scheme`), dry-run preview, then apply (failures stay visible in the modal). After creating a service, a homelab checklist links to Dockge, Uptime Kuma (`/add`), Homepage (copy-paste `services.yaml` snippet), and TiddlyWiki when those URLs are set in Settings.
- **Activity** — Append-only log of changes (stored as JSON lines under `/data/activity.jsonl`). When the log exceeds ~2 MB, **oldest** events are removed first (newest entries are kept) in a single pass, so trimming does not stall requests. The **Settings → Test connections** panel and **`GET /api/integrations/health`** report Cloudflare/NPM reachability. The header also shows in-memory **`/api/metrics`** counters (API requests and reconcile runs since container start).

**Architecture:**
```
Short links:         short.yourdomain.com/ha  →  https://ha.yourdomain.com
                     (Cloudflare edge — single-digit ms, always-on, no origin hit)

Management console:  urler.yourdomain.com
                     (internal only, behind your reverse proxy)
```

---

## Prerequisites

- **Cloudflare** managing your domain
- **Nginx Proxy Manager** running on your network
- **Docker + Docker Compose** on your server
- **OpenTofu** (or Terraform) for the one-time short-link bootstrap  
  → Install: https://opentofu.org/docs/intro/install/

> If you don’t have Docker installed yet: install Docker Engine + the Compose plugin for your OS (or Docker Desktop on Windows), then come back here.

---

## Quick Start

### 1. Deploy URLer

```bash
git clone https://github.com/BenSweaterVest/URL-ist.git ~/stacks/urler && cd ~/stacks/urler

# Optional but recommended: set a random SESSION_SECRET in compose.yaml
# (e.g. openssl rand -hex 32) so login cookies are stable across restarts.

docker compose up -d --build
```

Open `http://<your-server-ip>:8099` — the **setup wizard** will guide you through choosing a **URLer password** (this app only — not your OS or NPM account), then your Cloudflare and NPM credentials. Settings are saved to a persistent Docker volume (`/data/config.json`) and applied immediately — no container restart needed.

**After first setup:** Restart the container once so the session middleware uses the persisted `session_secret`:

```bash
docker compose restart
```

### 2. Register URLer in NPM (optional but recommended)

Once the wizard is complete, use the **Services** tab to add URLer itself:
- Subdomain: `urler`
- Backend host: `<your-server-ip>`
- Backend port: `8099`

This creates `urler.yourdomain.com` accessible via HTTPS through NPM.

### 3. Bootstrap short links (one-time Tofu setup)

The Short Links tab requires a one-time Cloudflare infrastructure setup. Until this is done, the tab shows a "bootstrap required" notice.

```bash
cd tofu/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — see "Cloudflare API Token" section below
tofu init
tofu plan    # review what will be created
tofu apply
```

After a successful `tofu apply`, the Short Links tab becomes active. All ongoing link management (add/delete) goes through the console UI — you don't need Tofu again unless you want to tear down the infrastructure.

> **Using an existing Tofu repo?** Copy `tofu/urls.tf` into your repo. The file is self-contained with its own provider, variables, and resources. Remove the `provider "cloudflare"` block if it's already defined elsewhere.

> **Short links in one picture:** after `tofu apply`, Cloudflare serves `https://short.yourdomain.com/{slug}` at the edge using your bulk redirect list. The console only adds or removes list entries; your homelab never receives that HTTP request.

---

## Cloudflare API Token

URLer requires a single Cloudflare API token. For short links (Tofu bootstrap), it needs additional account-level permissions.

**Required permissions:**

| Feature | Required Permission |
|---------|-------------------|
| DNS tab, Services tab | Zone > DNS > Edit |
| Short Links (Tofu bootstrap) | Account > Account Filter Lists > Edit |
| Short Links (Tofu bootstrap) | Account > Account Rulesets > Edit |

**Create or update a token:**  
`Cloudflare Dashboard → My Profile → API Tokens → Create Token`

All three permissions can be on a single token. After the Tofu bootstrap is complete, the `Account Filter Lists` and `Account Rulesets` permissions are no longer needed for day-to-day use (the console only writes list *items*, not the list structure itself). You can scope the token down afterward if you prefer.

**Finding your Account ID and Zone ID:**
- **Account ID**: Cloudflare dashboard → right sidebar of any zone page
- **Zone ID**: Cloudflare dashboard → your zone → Overview → API section (right sidebar)

---

## Configuration Reference

All settings are configured via the setup wizard on first launch. They can be changed later via the gear icon (⚙) in the top-right corner.

Settings are saved to `/data/config.json` inside the container (persisted via Docker volume).

| Setting | Description | Required |
|---------|-------------|----------|
| **CF API Token** | Cloudflare API token | ✓ |
| **CF Account ID** | Your Cloudflare account ID | ✓ |
| **CF Zone ID** | Zone ID for your domain | ✓ |
| **NPM URL** | Internal URL of your NPM instance, e.g. `http://192.168.1.1:81`. Use an IP address unless you also set DNS Target IP. | ✓ |
| **DNS Target IP** | IPv4 address that new service A records point to. Blank means "the host in NPM URL". Required when NPM URL uses a hostname, because Cloudflare only accepts an IP as A record content. | When NPM URL is a hostname |
| **NPM Email** | NPM admin email | ✓ |
| **NPM Password** | NPM admin password | ✓ |
| **Base Domain** | Your domain, e.g. `example.com` — new services become `subdomain.example.com` | ✓ |
| **Short Links Domain** | Full short link domain, e.g. `short.example.com` | For short links |
| **CF List Name** | Cloudflare list name — must match `cf_list_name` in `terraform.tfvars` | For short links |
| **NPM Cert ID** | ID of the wildcard SSL certificate in NPM (find in NPM → SSL Certificates). Must be **≥ 1** — `0` is not valid for service creation. | For new services |
| **Dockge URL** | Base URL of Dockge (optional; post-create checklist opens deploy UI) | Optional |
| **TiddlyWiki URL** | Base URL of your wiki (optional; post-create link to `#New Tiddler`) | Optional |
| **Uptime Kuma URL** | Base URL of Uptime Kuma (optional; post-create link to `/add` monitor form) | Optional |
| **Homepage URL** | Base URL of Homepage (optional; post-create copy-paste `services.yaml` snippet) | Optional |

**Editing `config.json` directly:** The app reads settings at startup and when you save via the UI. If you edit `/data/config.json` by hand, restart the container so middleware and in-memory config pick up changes (especially `session_secret`).

**Config history:** Each save via Settings keeps a snapshot under `/data/config-versions/` (see Security section for retention).

**URLer password:** Set in the wizard (stored as a salted hash in `config.json`). Minimum **12 characters**. Used only to unlock this web UI — it is **not** tied to your Ubuntu `sudo` password (the app runs in Docker and cannot safely verify OS logins). You may reuse the same passphrase if you want, but it is stored separately.

**Skipping the wizard (env vars):**  
Uncomment and populate the `environment:` section in `compose.yaml`, including **`CONSOLE_PASSWORD`**. On the first successful login the app writes a hash of that password to `config.json` and stops reading the env var. If you leave `CONSOLE_PASSWORD` out, the app starts in setup mode: only the wizard endpoints respond, and the UI asks you to choose a password before anything else works. Nothing is exposed without a password.

**Reopening the wizard:** if a required setting becomes invalid later (for example an NPM cert ID of `0`), the console shows the wizard again with your saved values filled in. Your console password is still required to get there, and secrets that come back masked are kept as they were unless you type new ones.

**Hostnames and IDs are normalised on save.** Domains lose any scheme, path, trailing slash or dot and are lowercased. Cloudflare account and zone IDs must be 32 hex characters. Typing `https://short.example.com/` into Short Links Domain therefore works as intended.

**Background reconcile (optional):** A periodic task compares NPM proxy hosts with Cloudflare DNS and logs drift. Tune with environment variables (see `compose.yaml` comments):

| Variable | Default | Purpose |
|----------|---------|---------|
| `RECONCILE_INTERVAL_SEC` | `3600` | Seconds between scans |
| `RECONCILE_INITIAL_DELAY_SEC` | `120` | Delay before the first scan after startup |
| `RECONCILE_WEBHOOK_URL` | (empty) | Optional **https://** webhook when unmatched or missing DNS is found |

Webhook JSON body: `{"event": "urler.reconcile", "unmatched_dns": <int>, "missing_dns_services": <int>}`.

Reconcile events appear in the **Activity** tab only when drift is detected (non-zero counts).

**Session signing:** `/api/health` and `/api/auth/status` include `session_secret_source` (`env`, `file`, or `runtime`) and `session_secret_restart_needed`. The source describes the key the running process is actually using, not what is on disk. Set **`SESSION_SECRET`** (≥16 characters) in `compose.yaml` for production, or let the wizard write `session_secret` to `config.json`. After the first wizard save the process is still on its temporary key, so `session_secret_restart_needed` is `true` and the getting-started banner asks you to **restart once**. The flag clears after the restart.

**NPM cert ID:** Must be **≥ 1** in Settings or `NPM_CERT_ID` env. A value of `0` marks the app as not fully configured and blocks service creation until corrected.

**Integrations health:** The header CF/NPM pills use a lightweight parallel probe (one DNS record + NPM settings read), not a full proxy-host listing.

---

## How It Works

### Short Links

Short links use [Cloudflare Bulk Redirects](https://developers.cloudflare.com/rules/url-forwarding/bulk-redirects/) — a native Cloudflare feature that runs at the edge with no origin server involved:

```
User visits short.example.com/ha
  → Cloudflare intercepts (edge node, near the user)
  → Looks up /ha in your redirect list
  → Returns 302 to https://ha.example.com (default; API also accepts 301/307/308)
  → User follows redirect directly to destination
```

New links use **302** (temporary redirect) so you can change a slug’s destination later; **301** is cached aggressively by browsers and makes edits invisible to repeat visitors. Existing list items keep their original status code when you edit them.

The authenticated API **`GET /api/links/preflight`** returns whether the redirect list exists, whether the short-domain **proxied A** record looks correct, and a short list of human-readable issues (the Short Links tab uses this).

Your home server is never contacted for short link redirects. The console only needs to be running when you *manage* (add/edit/delete) links — not for the redirects themselves.

> **Important:** Do not delete your configured short-links domain A record from the DNS tab. If removed, short links stop working until the record is recreated (typically by re-running `tofu apply`).

**Availability**: Short links work even if your home server is offline.  
**Latency**: Single-digit milliseconds from any location (Cloudflare edge node near the user, not your home upload speed).  
**Cost**: Uses Cloudflare Bulk Redirects on the free plan (no Workers). Redirect list capacity varies by plan — if you hit a quota error, check your account limits in the Cloudflare dashboard. API errors from Cloudflare are shown in the UI with the message returned by Cloudflare.

**Writes are applied in the background by Cloudflare.** Adding, editing or deleting a list item returns an operation ID rather than the finished item. The console polls that operation for up to ten seconds before reporting success, which is how errors such as a duplicate slug reach the UI. If Cloudflare is slow the response carries `pending: true` and the UI shows a warning; refresh a moment later.

**Items created elsewhere:** a list item with no explicit status code is treated by Cloudflare as a 301, and the console shows and preserves it as 301. Links created through the console always carry an explicit code.

### Services tab

Creates two things in sequence, with automatic DNS rollback if NPM fails:

1. A Cloudflare A record: `subdomain.yourdomain.com → your-npm-ip`
2. An NPM proxy host: `subdomain.yourdomain.com → scheme://backend-host:port` with your wildcard SSL cert

**Delete** removes the NPM proxy host and matching DNS record (warns if one step fails). **Enable/disable** calls NPM's own enable and disable endpoints, so no other setting on the proxy host is touched. **Batch import** accepts JSON (`[{ "subdomain", "forward_host", "forward_port", ... }]`), supports dry-run preview, and optional continue-on-error.

Subdomains must be a single DNS label: 1 to 63 lowercase letters, digits or hyphens, not starting or ending with a hyphen.

If NPM proxy host creation fails, the DNS record is automatically rolled back. If you need to retry a partially-failed service creation, delete any orphaned DNS record from the DNS tab first.

In **Settings**, use **Auto-detect wildcard cert** (after the app is configured) to fill NPM Cert ID from NPM. The first-run wizard does not call this endpoint until setup is complete.

### Tofu / console split

Tofu creates the **infrastructure** once (the redirect list, the ruleset, the short domain DNS record). These are permanent resources that never need to change.

URLer manages **data** — the entries in the list, new DNS records, new NPM hosts. These are runtime operations that happen frequently and don't belong in version-controlled HCL.

If you're already using Tofu to manage DNS records for your domain, records created via the console won't be in your Tofu state. That's intentional: the console is the source of truth for additions going forward. `tofu plan` will not touch records it doesn't know about.

---

## Architecture Details

```
┌──────────────────────────────────────────────────────┐
│                    Your Server                       │
│                                                      │
│  ┌──────────────────┐    ┌──────────────────────┐   │
│  │  Console App     │    │  Nginx Proxy Manager  │   │
│  │  FastAPI + React │    │  (reverse proxy)      │   │
│  │  port 8099       │───▶│  port 80/443          │   │
│  └────────┬─────────┘    └──────────────────────┘   │
│           │                                          │
└───────────┼──────────────────────────────────────────┘
            │  API calls (HTTPS)
            ▼
   ┌─────────────────────┐
   │   Cloudflare API    │
   │  • DNS records      │
   │  • Redirect list    │
   │  • Ruleset          │
   └─────────────────────┘
```

**Stack:** Python 3.12 / FastAPI backend + React 18 SPA (CDN, no build step) served as static files from the same container.

**Data flow:** The frontend makes API calls to the FastAPI backend, which proxies them to the Cloudflare and NPM APIs. No data is stored by the console other than credentials in `/data/config.json`.

---

## Deployment Notes

### Running behind NPM

URLer itself can be proxied through NPM like any other service:

1. Use the **Services** tab to create `urler.yourdomain.com` pointing to `<your-server-ip>:8099`
2. Or add it manually in NPM: domain `urler.yourdomain.com`, forward to `http://<ip>:8099`, enable your wildcard cert

No special NPM settings needed (no WebSocket support, no `proxy_ssl_verify off`).

If you proxy through NPM over HTTPS, set these in `compose.yaml`:

- `SESSION_COOKIE_HTTPS_ONLY=1`
- `TRUST_PROXY_HEADERS=1` (so login rate limiting uses real client IPs)

### Persistent configuration

Configuration is stored in a Docker named volume (`urler_data` mounted at `/data`). To use a host path instead (easier to back up):

```yaml
# In compose.yaml, replace the volumes section at the bottom:
volumes:
  urler_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /your/host/path/data
```

### Updating

```bash
docker compose pull   # if using a registry image
docker compose build  # if building locally
docker compose up -d
```

Configuration in `/data/config.json` is preserved across updates.

---

## Repository Structure

```
├── main.py                 FastAPI backend
├── CHANGELOG.md            User-visible changes per version
├── requirements.txt        Python dependencies
├── requirements-dev.txt    pytest, ruff (local/CI)
├── tests/                  API tests (auth, CSRF, links, preflight)
├── scripts/                Helper scripts (e.g. vendor frontend assets)
├── Dockerfile
├── compose.yaml            Docker Compose stack
├── .dockerignore           Prevents secrets leaking into Docker build context
├── .gitignore              Excludes config.json, Tofu secrets, venv
├── static/
│   └── index.html          React SPA (wizard + main app)
└── tofu/
    ├── urls.tf             Cloudflare infrastructure bootstrap
    ├── terraform.tfvars.example
    └── .gitignore          Excludes terraform.tfvars and state files
```

**API auth:** Two checks apply to `/api/*` routes other than `/api/health` and `/api/auth/*`:

1. **Setup mode.** While a required setting is missing or no console password exists, only the wizard endpoints respond (`GET`/`POST /api/config`, `GET /api/config/status`, `POST /api/config/test`). Everything else returns 503.
2. **Login.** As soon as a console password exists, every remaining route needs a logged-in session and a CSRF token on `POST`/`PUT`/`DELETE`. This applies inside setup mode too, so a half-configured install cannot be reconfigured by someone who does not know the password.

Only a brand-new install with no password yet reaches the wizard without a session. Unknown `/api/*` paths return a JSON 404.

---

## Security

URLer manages your Cloudflare DNS and API credentials. Keep it internal.

**Access control**: Do not expose port 8099 to the public internet. The app requires a **console password** and a **signed session cookie** for all API calls after setup. Still treat network access as part of your trust boundary: combine the password with LAN-only or VPN-only reachability, firewall rules, and a strong session signing key.

**Session signing**: On first successful wizard save, a random **`session_secret`** is written into `config.json` (unless **`SESSION_SECRET`** is already set in the environment, ≥16 characters). If neither is present, the app uses a **per-process random** session key (secure, but sessions reset on restart). For stable sessions across restarts, set `SESSION_SECRET` in Compose or complete the wizard once and then restart so the middleware loads the persisted key.

**HTTPS behind NPM**: When the browser only reaches the app over `https://`, set **`SESSION_COOKIE_HTTPS_ONLY=1`** in Compose so the session cookie is not sent on accidental `http://` hits.

**Cookie settings**: By default the session cookie is **HttpOnly** (not accessible to JavaScript) and uses `SameSite=Lax`. You can set **`SESSION_COOKIE_SAMESITE=strict`** if you want stricter cross-site behavior.

**Sign out does not revoke old cookies.** Sessions are signed cookies with no server-side record, so a cookie copied before sign-out stays valid until it expires (14 days). To invalidate every session at once, change `SESSION_SECRET` (or the `session_secret` in `config.json`) and restart.

**CSRF protection**: All state-changing API requests require an `X-CSRF-Token` header (the SPA handles this automatically after login).

**Login rate limiting**: `/api/auth/login` is limited per client IP (in-memory, resets on container restart) to slow password guessing.

**Behind NPM**: If you proxy through NPM, enable **`TRUST_PROXY_HEADERS=1`** so login rate limiting uses the **last** (trusted) IP in `X-Forwarded-For` — the hop your proxy appended. Configure NPM to **overwrite** `X-Forwarded-For` rather than appending to a client-supplied value, or rate limits can be bypassed.

**Backups**: Back up the Docker volume (or host bind mount) that holds **`/data/config.json`** — it contains Cloudflare/NPM credentials and your console password hash.

**Config history**: URLer keeps a rolling history of prior configs under **`/data/config-versions/`** (default: last 100). You can change retention with `CONFIG_HISTORY_LIMIT` (set to `0` to disable).

**NPM proxying**: If you expose the console via NPM (`urler.yourdomain.com`), ensure that DNS record is **not** publicly accessible — point it to your NPM's internal IP only, and do not open inbound firewall ports for it. Or use Tailscale for external access instead.

**Credentials at rest**: `config.json` is stored with `0o600` permissions (owner read/write only) inside the container. The volume should be on encrypted storage if your threat model requires it.

**Cloudflare token scoping**: Once Tofu bootstrap is complete, you can remove the `Account Filter Lists` and `Account Rulesets` permissions from your token — the console only writes list *items*, not the list structure. A narrower token limits blast radius if credentials are ever exposed.

**Air-gapped / offline deployments**: The frontend loads React and fonts from CDN by default. For fully offline use, run **`scripts/vendor-frontend.sh`** (or copy the `curl` commands inside it), then point the `<script src>` tags in `static/index.html` at `/static/vendor/...`.

---

## What not to commit

This repository is intended for public GitHub. **Never commit:**

| Never commit | Why |
|--------------|-----|
| `config.json`, `/data/*` exports | CF/NPM tokens, console password hash, `session_secret` |
| `terraform.tfvars`, `.env` | Cloudflare tokens and deploy secrets |
| `AGENTS.local.md`, `AGENTS.md`, `*.local.md` | Homelab hostnames, LAN IPs, personal domains |
| `activity.jsonl`, `service-trash.json` | Runtime logs may include domains you manage |

Use **`AGENTS.example.md`** → copy to **`AGENTS.local.md`** (or `AGENTS.md`) for Cursor/agent context with your real values. **`AGENTS.md`**, **`AGENTS.local.md`**, and **`*.local.md`** are gitignored — never commit homelab IPs or domains.

Before pushing: `git status` and confirm no secrets or homelab-specific notes are staged. Test fixtures use fake tokens like `cfat_test` only.

---

## Contributing

Pull requests welcome. A few notes:

- The frontend uses React via CDN + Babel standalone (no build step). Keep it that way — the goal is a single deployable folder with no Node.js dependency.
- The backend uses FastAPI with minimal dependencies. Check `requirements.txt` before adding new packages.
- The Tofu file targets Cloudflare provider `~> 4.0`. The v4 API has breaking changes from v3 — don't backport.
- Credentials never go in code. Test with real-looking but obviously fake values in examples.

**Tests** (use **Python 3.12**, same as the Docker image — 3.14+ may lack prebuilt wheels for pinned dependencies):

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

---

## License

MIT
