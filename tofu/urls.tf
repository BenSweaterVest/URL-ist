# urls.tf — URLer: Short Link Infrastructure Bootstrap
# ===================================================================
# Run this once before using the Short Links tab in the console.
# It creates three permanent Cloudflare resources:
#
#   1. A proxied DNS A record for your short link domain
#      (e.g. short.example.com → 192.0.2.1)
#
#   2. A Cloudflare Bulk Redirect List (container for redirect entries)
#      The console app writes/deletes entries in this list at runtime.
#
#   3. An account-level Bulk Redirect ruleset
#      Activates the list so redirects actually fire at Cloudflare's edge.
#
# After a successful apply, all short link management goes through the
# console UI — you do NOT need to run Tofu again to add/remove links.
#
# ── Prerequisites ─────────────────────────────────────────────────────
#
#   1. Install OpenTofu:  https://opentofu.org/docs/intro/install/
#   2. Copy terraform.tfvars.example to terraform.tfvars and fill it in.
#   3. Ensure your CF API token has:
#        Zone > DNS > Edit
#        Account > Account Filter Lists > Edit
#        Account > Account Rulesets > Edit
#
# ── Usage ─────────────────────────────────────────────────────────────
#
#   cd tofu/
#   tofu init
#   tofu plan     # review what will be created
#   tofu apply
#
# ── Existing ruleset? ─────────────────────────────────────────────────
#
# Cloudflare allows only ONE http_request_redirect ruleset per account.
# If you already have one, import it before applying:
#
#   tofu import cloudflare_ruleset.shortlinks <your-existing-ruleset-id>
#
# Find the existing ruleset ID in the Cloudflare dashboard under:
#   Account > Rules > Redirect Rules > Bulk Redirect Rules
#
# ── Integrating into an existing Tofu repo ────────────────────────────
#
# If you already use Tofu/Terraform to manage this Cloudflare account,
# copy this file into your repo and set the variables using your existing
# var/tfvars setup.  The provider block and variables below can be omitted
# if they're already defined elsewhere.

terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}


# ── Variables ─────────────────────────────────────────────────────────

variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit + Account:Account Filter Lists:Edit + Account:Account Rulesets:Edit permissions."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID. Found in the Cloudflare dashboard sidebar on any zone page."
  type        = string

  validation {
    condition     = can(regex("^[a-f0-9]{32}$", var.cloudflare_account_id))
    error_message = "Account ID must be a 32-character hex string (no hyphens). Copy it exactly from the Cloudflare dashboard."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for your domain. Found on the zone Overview page in the API section."
  type        = string

  validation {
    condition     = can(regex("^[a-f0-9]{32}$", var.cloudflare_zone_id))
    error_message = "Zone ID must be a 32-character hex string (no hyphens). Copy it exactly from the Cloudflare dashboard."
  }
}

variable "short_subdomain" {
  description = "Subdomain to use for short links. For example, 'short' creates short.yourdomain.com."
  type        = string
  default     = "short"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9\\-]*$", var.short_subdomain))
    error_message = "Subdomain must be lowercase alphanumeric and hyphens only."
  }
}

variable "cf_list_name" {
  description = "Name for the Cloudflare redirect list. Must match the CF_LIST_NAME setting in the console."
  type        = string
  default     = "shortlinks"

  validation {
    condition     = can(regex("^[a-z0-9_]+$", var.cf_list_name))
    error_message = "Cloudflare list names must contain only lowercase letters, numbers, and underscores. No hyphens."
  }
}


# ── Resources ──────────────────────────────────────────────────────────

# DNS A record for the short link subdomain.
#
# 192.0.2.1 is an RFC 5737 documentation address — traffic never reaches it
# because the Bulk Redirect ruleset intercepts the request at Cloudflare's
# edge before any origin lookup occurs.
#
# The record MUST be proxied (orange cloud) for Cloudflare to see the traffic
# and apply redirect rules. Unproxied records bypass Cloudflare entirely.
resource "cloudflare_record" "short_domain" {
  zone_id = var.cloudflare_zone_id
  name    = var.short_subdomain
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  comment = "Short link domain — managed by console Tofu bootstrap. Do not edit manually."

  lifecycle {
    prevent_destroy = true
  }
}


# Bulk Redirect List — the container for URL redirect entries.
#
# The console app populates this list at runtime via the Cloudflare API.
# The list_id output below can optionally be noted; the console discovers
# it automatically by name on first use.
resource "cloudflare_list" "shortlinks" {
  account_id  = var.cloudflare_account_id
  name        = var.cf_list_name
  description = "Short link redirects managed by the console app"
  kind        = "redirect"

  # Protect against accidental destruction.  The list contains all your
  # short link entries — destroying it would delete every redirect instantly.
  # To intentionally remove this resource: comment out these lines first.
  lifecycle {
    prevent_destroy = true
  }
}


# Account-level Bulk Redirect ruleset.
#
# This activates the redirect list so that matching requests are redirected
# at the Cloudflare edge.  The ruleset applies globally across all zones
# in your Cloudflare account.
#
# Constraint: only ONE http_request_redirect ruleset can exist at the
# account level.  If you already have one, import it (see header comments).
#
# Expression syntax note:
#   CF expressions use "$listname" to reference a list.  In HCL, "$" before
#   "{" triggers interpolation, so we use ${"$"} to produce a literal "$",
#   then interpolate the list name separately.
#   Result: http.request.full_uri in $shortlinks
resource "cloudflare_ruleset" "shortlinks" {
  account_id  = var.cloudflare_account_id
  name        = "Short Links"
  description = "Bulk redirect ruleset for ${var.short_subdomain} short links"
  kind        = "root"
  phase       = "http_request_redirect"

  rules {
    action      = "redirect"
    description = "Redirect matching requests via bulk redirect list"
    enabled     = true

    # "$listname" is the CF expression for a bulk redirect list.
    # See expression syntax note above.
    expression = "http.request.full_uri in ${"$"}${cloudflare_list.shortlinks.name}"

    action_parameters {
      from_list {
        name = cloudflare_list.shortlinks.name
        key  = "http.request.full_uri"
      }
    }
  }

  depends_on = [cloudflare_list.shortlinks]

  # Protect against accidental destruction.  Removing the account-level ruleset
  # would disable ALL bulk redirects for the entire Cloudflare account instantly.
  lifecycle {
    prevent_destroy = true
  }
}


# ── Outputs ───────────────────────────────────────────────────────────

output "short_subdomain_created" {
  description = "The short link subdomain that was created. Append your domain to form the full URL."
  value       = var.short_subdomain
}

output "cf_list_id" {
  description = "Cloudflare redirect list ID. The console discovers this automatically — noted here for reference."
  value       = cloudflare_list.shortlinks.id
}

output "cf_ruleset_id" {
  description = "Cloudflare account-level redirect ruleset ID. Store this if you may need to import it later."
  value       = cloudflare_ruleset.shortlinks.id
}

output "dns_record_id" {
  description = "Cloudflare DNS record ID for the short link subdomain."
  value       = cloudflare_record.short_domain.id
}
