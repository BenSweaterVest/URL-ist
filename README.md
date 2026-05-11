# PenguinNest Console

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

Management console:  console.yourdomain.com
                     (internal only, behind your reverse proxy)
```

---

## Prerequisites

- **Cloudflare** managing your domain
- **Nginx Proxy Manager** running on your network
- **Docker + Docker Compose** on your server
- **OpenTofu** (or Terraform) for the one-time short-link bootstrap  
  → Install: https://opentofu.org/docs/intro/install/

---

## Quick Start

### 1. Deploy the console

```bash
# Create the stack directory
mkdir ~/stacks/console && cd ~/stacks/console

# Download the app files (or clone the repo)
# Place: main.py, requirements.txt, Dockerfile, compose.yaml, static/index.html

# Build and start
docker compose up -d
```

Open `http://<your-server-ip>:8099` — the **setup wizard** will guide you through entering your Cloudflare and NPM credentials. Settings are saved to a persistent Docker volume (`/data/config.json`) and applied immediately — no container restart needed.

### 2. Register the console in NPM (optional but recommended)

Once the wizard is complete, use the **Services** tab to add the console itself:
- Subdomain: `console`
- Backend host: `<your-server-ip>`
- Backend port: `8099`

This creates `console.yourdomain.com` accessible via HTTPS through NPM.

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

---

## Cloudflare API Token

The console requires a single Cloudflare API token. For short links (Tofu bootstrap), it needs additional account-level permissions.

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

**Skipping the wizard (env vars):**  
Uncomment and populate the `environment:` section in `compose.yaml`. The app will start fully configured and skip the wizard.

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

Your home server is never contacted for short link redirects. The console only needs to be running when you *manage* (add/delete) links — not for the redirects themselves.

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

The console manages **data** — the entries in the list, new DNS records, new NPM hosts. These are runtime operations that happen frequently and don't belong in version-controlled HCL.

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

The console itself can be proxied through NPM like any other service:

1. Use the **Services** tab to create `console.yourdomain.com` pointing to `<your-server-ip>:8099`
2. Or add it manually in NPM: domain `console.yourdomain.com`, forward to `http://<ip>:8099`, enable your wildcard cert

No special NPM settings needed (no WebSocket support, no `proxy_ssl_verify off`).

### Persistent configuration

Configuration is stored in a Docker named volume (`urlconsole_data` mounted at `/data`). To use a host path instead (easier to back up):

```yaml
# In compose.yaml, replace the volumes section at the bottom:
volumes:
  urlconsole_data:
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

This console manages your Cloudflare DNS and API credentials. Keep it internal.

**Access control**: Do not expose port 8099 publicly. The console has no user authentication — anyone who can reach it has full control over DNS records and short links. Keep it behind your LAN or a VPN.

**NPM proxying**: If you expose the console via NPM (`console.yourdomain.com`), ensure that DNS record is **not** publicly accessible — point it to your NPM's internal IP only, and do not open inbound firewall ports for it. Or use Tailscale for external access instead.

**Credentials at rest**: `config.json` is stored with `0o600` permissions (owner read/write only) inside the container. The volume should be on encrypted storage if your threat model requires it.

**Cloudflare token scoping**: Once Tofu bootstrap is complete, you can remove the `Account Filter Lists` and `Account Rulesets` permissions from your token — the console only writes list *items*, not the list structure. A narrower token limits blast radius if credentials are ever exposed.

**Air-gapped / offline deployments**: The frontend loads React and fonts from CDN by default. For fully offline use, download the three script files and save them to `static/` (alongside `index.html`), then update the `<script src>` tags to use relative paths:

```bash
curl -o static/react.production.min.js https://unpkg.com/react@18.3.1/umd/react.production.min.js
curl -o static/react-dom.production.min.js https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js
curl -o static/babel.min.js https://unpkg.com/@babel/standalone@7.26.10/babel.min.js
```

Then replace the three `<script src="https://unpkg.com/...">` tags in `static/index.html` with local equivalents (`src="react.production.min.js"` etc.).

---

## Contributing

Pull requests welcome. A few notes:

- The frontend uses React via CDN + Babel standalone (no build step). Keep it that way — the goal is a single deployable folder with no Node.js dependency.
- The backend uses FastAPI with minimal dependencies. Check `requirements.txt` before adding new packages.
- The Tofu file targets Cloudflare provider `~> 4.0`. The v4 API has breaking changes from v3 — don't backport.
- Credentials never go in code. Test with real-looking but obviously fake values in examples.

---

## License

MIT
