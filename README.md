# URLer

A self-hosted management console for [Cloudflare](https://cloudflare.com) + [Nginx Proxy Manager](https://nginxproxymanager.com), built for homelab use.

**Three panels, one app:**

| Panel | What it does |
|-------|-------------|
| **Short Links** | Create and manage `short.yourdomain.com/slug → destination` redirects via Cloudflare Bulk Redirects — runs at Cloudflare's edge, never touches your server |
| **DNS** | View, add, and delete Cloudflare A records for your zone |
| **Services** | Add a new self-hosted service in one form: creates the DNS A record and the NPM proxy host together |

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
git clone https://github.com/YOUR_USER/urler.git ~/stacks/urler && cd ~/stacks/urler

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
| **NPM URL** | Internal URL of your NPM instance, e.g. `http://192.168.1.1:81` | ✓ |
| **NPM Email** | NPM admin email | ✓ |
| **NPM Password** | NPM admin password | ✓ |
| **Base Domain** | Your domain, e.g. `example.com` — new services become `subdomain.example.com` | ✓ |
| **Short Links Domain** | Full short link domain, e.g. `short.example.com` | For short links |
| **CF List Name** | Cloudflare list name — must match `cf_list_name` in `terraform.tfvars` | For short links |
| **NPM Cert ID** | ID of the wildcard SSL certificate in NPM (find in NPM → SSL Certificates) | For new services |

**URLer password:** Set in the wizard (stored as a salted hash in `config.json`). Used only to unlock this web UI — it is **not** tied to your Ubuntu `sudo` password (the app runs in Docker and cannot safely verify OS logins). You may reuse the same passphrase if you want, but it is stored separately.

**Skipping the wizard (env vars):**  
Uncomment and populate the `environment:` section in `compose.yaml`. The app will start fully configured and skip the wizard. Set **`CONSOLE_PASSWORD`** there if you need a plaintext bootstrap password before the first save to `config.json` (on first successful login, a hash is written and the env-only path is no longer used).

**Session signing:** Set **`SESSION_SECRET`** (long random string, ≥16 characters) in `compose.yaml` so session cookies cannot be forged by anyone who can reach the container port. Optional `session_secret` in `config.json` is used as a fallback if `SESSION_SECRET` is not set.

If you do not set `SESSION_SECRET`, the wizard will generate and persist a `session_secret` into `config.json`. **Restart once after setup** so the middleware loads that persisted key (otherwise sessions are signed with a per-process random key and will reset on restart).

---

## How It Works

### Short Links

Short links use [Cloudflare Bulk Redirects](https://developers.cloudflare.com/rules/url-forwarding/bulk-redirects/) — a native Cloudflare feature that runs at the edge with no origin server involved:

```
User visits short.example.com/ha
  → Cloudflare intercepts (edge node, near the user)
  → Looks up /ha in your redirect list
  → Returns 301 to https://ha.example.com
  → User follows redirect directly to destination
```

The authenticated API **`GET /api/links/preflight`** returns whether the redirect list exists, whether the short-domain **proxied A** record looks correct, and a short list of human-readable issues (the Short Links tab uses this).

Your home server is never contacted for short link redirects. The console only needs to be running when you *manage* (add/delete) links — not for the redirects themselves.

> **Important:** Do not delete your configured short-links domain A record from the DNS tab. If removed, short links stop working until the record is recreated (typically by re-running `tofu apply`).

**Availability**: Short links work even if your home server is offline.  
**Latency**: Single-digit milliseconds from any location (Cloudflare edge node near the user, not your home upload speed).  
**Cost**: Free tier covers personal use (no Workers, just native Cloudflare rules).

### Services tab

Creates two things in sequence, with automatic DNS rollback if NPM fails:

1. A Cloudflare A record: `subdomain.yourdomain.com → your-npm-ip`
2. An NPM proxy host: `subdomain.yourdomain.com → scheme://backend-host:port` with your wildcard SSL cert

If NPM proxy host creation fails, the DNS record is automatically rolled back. If you need to retry a partially-failed service creation, delete any orphaned DNS record from the DNS tab first.

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
├── requirements.txt        Python dependencies
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

---

## Security

URLer manages your Cloudflare DNS and API credentials. Keep it internal.

**Access control**: Do not expose port 8099 to the public internet. The app requires a **console password** and a **signed session cookie** for all API calls after setup. Still treat network access as part of your trust boundary: combine the password with LAN-only or VPN-only reachability, firewall rules, and a strong session signing key.

**Session signing**: On first successful wizard save, a random **`session_secret`** is written into `config.json` (unless **`SESSION_SECRET`** is already set in the environment, ≥16 characters). If neither is present, the app uses a **per-process random** session key (secure, but sessions reset on restart). For stable sessions across restarts, set `SESSION_SECRET` in Compose or complete the wizard once and then restart so the middleware loads the persisted key.

**HTTPS behind NPM**: When the browser only reaches the app over `https://`, set **`SESSION_COOKIE_HTTPS_ONLY=1`** in Compose so the session cookie is not sent on accidental `http://` hits.

**Cookie settings**: By default the session cookie is **HttpOnly** (not accessible to JavaScript) and uses `SameSite=Lax`. You can set **`SESSION_COOKIE_SAMESITE=strict`** if you want stricter cross-site behavior.

**CSRF protection**: All state-changing API requests require an `X-CSRF-Token` header (the SPA handles this automatically after login).

**Login rate limiting**: `/api/auth/login` is limited per client IP (in-memory, resets on container restart) to slow password guessing.

**Behind NPM**: If you proxy through NPM, enable **`TRUST_PROXY_HEADERS=1`** so rate limiting keys off the real client IP from `X-Forwarded-For` (only do this behind a trusted reverse proxy).

**Backups**: Back up the Docker volume (or host bind mount) that holds **`/data/config.json`** — it contains Cloudflare/NPM credentials and your console password hash.

**Config history**: URLer keeps a rolling history of prior configs under **`/data/config-versions/`** (default: last 100). You can change retention with `CONFIG_HISTORY_LIMIT` (set to `0` to disable).

**NPM proxying**: If you expose the console via NPM (`console.yourdomain.com`), ensure that DNS record is **not** publicly accessible — point it to your NPM's internal IP only, and do not open inbound firewall ports for it. Or use Tailscale for external access instead.

**Credentials at rest**: `config.json` is stored with `0o600` permissions (owner read/write only) inside the container. The volume should be on encrypted storage if your threat model requires it.

**Cloudflare token scoping**: Once Tofu bootstrap is complete, you can remove the `Account Filter Lists` and `Account Rulesets` permissions from your token — the console only writes list *items*, not the list structure. A narrower token limits blast radius if credentials are ever exposed.

**Air-gapped / offline deployments**: The frontend loads React and fonts from CDN by default. For fully offline use, run **`scripts/vendor-frontend.sh`** (or copy the `curl` commands inside it), then point the `<script src>` tags in `static/index.html` at `/static/vendor/...`.

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
