# URLer — Agent context (template)

Copy to **`AGENTS.local.md`** (gitignored) and fill in your homelab values.  
Do **not** commit real domains, LAN IPs, API tokens, or passwords.

```
cp AGENTS.example.md AGENTS.local.md
```

In Cursor, reference `@AGENTS.local.md` (or merge into a personal rule). The committed **`AGENTS.example.md`** stays generic.

## Goal

**URLer** replaces a shell “add service” script and centralizes Cloudflare + NPM. Success = DNS + NPM in one UI, plus **checklist handoffs** for Dockge → Homepage → Uptime Kuma → wiki (links/snippets only — no external APIs).

## Typical homelab topology (customize in AGENTS.local.md)

| Piece | Example role |
|-------|----------------|
| Docker host | `10.0.0.x` — stacks under `~/stacks/` |
| NPM host | `10.0.0.y:81` — all `*.example.com` A records point here |
| Domain | `example.com` |
| URLer | `https://urler.example.com` (internal; do not expose :8099 publicly) |

**Rules:** DNS A records → NPM IP only; Tofu may manage some DNS — URLer runtime DNS is CF API (not in Tofu state); RFC1918 NPM URLs must stay allowed.

## New-service workflow

| Step | URLer |
|------|--------|
| 0 | Services tab — DNS + NPM |
| 1–4 | Post-create checklist: Dockge, Homepage YAML copy, Kuma `/add`, wiki tiddler |

## Product (public repo facts)

- FastAPI + React SPA (CDN/Babel, no build)
- Tabs: Short Links (Tofu bootstrap once), DNS, Services, Activity
- Secrets: `/data/config.json` on volume only — never in git

## Key files

`main.py`, `static/index.html`, `tests/`, `compose.yaml`, `README.md`, `tofu/`

## Implementation notes (public repo)

- `npm_cert_id < 1` → `is_configured()` is false (no silent default to 2).
- Activity log trims **oldest** lines when over size cap.
- Post-create checklist: Dockge, Homepage YAML copy, Kuma `/add`, wiki tiddler (Settings URLs).

## Agent tips

1. Read `README.md` first.
2. Never commit `config.json`, `.env`, `terraform.tfvars`, or real tokens.
3. `GET /api/dns` → `{ records, truncated, npm_proxy_domains }`; `GET /api/scan` includes `domain`.
4. Only `git commit` when the user asks.
