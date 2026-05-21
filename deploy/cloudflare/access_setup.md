# Cloudflare Access for the AlphaLens deployment

Operator runbook for fronting the AlphaLens stack (SPA at `/` + REST API
at `/api/*`) with **Cloudflare Tunnel + Access** using a **single
hostname** and **whole-hostname Access policy**.

Why both layers:
- **Tunnel** maps a public hostname onto a private origin (the web and
  api containers bind to `127.0.0.1` on the VPS — no port is exposed to
  the public internet).
- **Access** is the auth layer — Google SSO for browser users, Service
  Tokens for bots / scripts. The application stays auth-free; Cloudflare
  validates identity at the edge before forwarding.

Free Zero Trust tier (up to 50 users) covers everything below.

> **Where this is live:** this repo's production runs on
> `alphalens.kamilpajak.pl`. The instructions below use placeholder
> `<host>` so they work for a fresh deploy too; substitute your hostname
> consistently.

---

## Architectural decision: single hostname, whole-hostname Access

The SPA at `/` and the REST API at `/api/*` share **one hostname**
(`<host>`) and **one Cloudflare Access policy** covering the entire
hostname. The web container's nginx reverse-proxies `/api/*` to the api
container on the docker-compose internal network; both serve from the
same public origin.

We considered and **rejected** two alternatives:

1. **Separate `api.<host>` subdomain** — would have given clean per-app
   policies but split the cookie domain. The browser SPA at `<host>`
   making fetches to `api.<host>` would face cross-origin CORS + cookie
   complications, plus operators have to manage two tunnel ingress rules
   and two Access apps for one logical product.
2. **Path-scoped Access only on `<host>/api/*`** (SPA at `/` left
   anonymous) — broke the natural browser journey: visiting `<host>` set
   no Access cookie; SPA JS calling `fetch('/api/v1/days')` got a 302 to
   `<team>.cloudflareaccess.com` (cross-origin), and browser `fetch` API
   cannot follow cross-origin redirects from JS. SPA shell loaded, data
   never did. We deployed this briefly on 2026-05-21 before reverting
   to whole-hostname.

Whole-hostname matches the pattern most managed dashboards use
(Grafana, Sentry, Linear admin, AWS console, etc.) — log in once at the
hostname, single cookie covers SPA + API, single 24h session.

---

## 1. Tunnel ingress

If you already run a `cloudflared` tunnel on the VPS, add **one** ingress
rule for the hostname — pointing at the **web container's port** (the
web container's nginx handles `/api/` proxying internally; do not expose
the api container to the tunnel).

Edit the tunnel config (commonly `~/.cloudflared/config.yml` or
`/etc/cloudflared/config.yml`):

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/<user>/.cloudflared/<your-tunnel-id>.json

ingress:
  # AlphaLens (SPA + API behind nginx /api/ proxy)
  - hostname: <host>
    service: http://127.0.0.1:8085
  - service: http_status:404
```

Apply:

```bash
sudo systemctl restart cloudflared   # OR `cloudflared tunnel run` if attached
cloudflared tunnel info <your-tunnel-id>
```

Add the public DNS record (Cloudflare dashboard → DNS):

```
<host>  CNAME  <tunnel-id>.cfargotunnel.com  (Proxied)
```

The api container's `127.0.0.1:8086:8000` binding stays on the VPS only
— used for SSH-tunnel debugging (`ssh -L 8086:127.0.0.1:8086 <user>@<vps>`)
and never reached from the internet.

---

## 2. Access application (whole hostname)

Cloudflare dashboard → **Zero Trust** → **Access** → **Applications** →
**Add an application** → **Self-hosted**.

| Field | Value |
|---|---|
| Application name | `AlphaLens` |
| Session duration | 24 hours |
| Application domain | `<host>` *(no path suffix — protects the whole hostname including SPA, API, Swagger)* |
| Identity providers | enable **Google** (and/or any other IdP you've configured) |
| Auto-redirect to identity | **On** — skips the IdP picker when only one identity-based IdP is enabled |
| Skip interstitial | **On** — bypasses the "you're being redirected" page |
| App launcher visible | **Off** — internal app, no need in the Cloudflare app launcher |

Save the app. Cloudflare assigns it an `aud` (audience) identifier; if
you later add JWT verification inside the application layer, this is the
value you'll check (see §6 Notes / deferred).

---

## 3. Policies (two — one per actor type)

### Policy A — operator browser SSO

| Field | Value |
|---|---|
| Policy name | `Allow Kamil (Google SSO)` |
| Action | **Allow** *(decision: `allow`)* |
| Include | Emails → `<your-email@example.com>` |
| Require | (empty — single user; add Country / Auth method gates here if needed) |

### Policy B — service tokens (bots, scripts, CI)

| Field | Value |
|---|---|
| Policy name | `Service Auth: <service-name>` |
| Action | **Service Auth** *(decision: `non_identity`)* |
| Include | Service Token → (pick the token created in §4) |

Save both. The Allow-email policy short-circuits the SSO flow for
operator users; the Service-Auth policy lets any caller presenting
matching `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers
through without invoking the IdP.

> **Why `non_identity` for Service Tokens?** Cloudflare flags Service
> Token authentication as identity-less (no human, no IdP-issued JWT).
> Mixing it with a `decision: allow` policy works but is more permissive
> than needed. The dedicated `non_identity` decision tells Cloudflare
> "this policy applies only when the request comes via Service Token."

---

## 4. Service Token issuance

Cloudflare dashboard → **Zero Trust** → **Access** → **Service Auth** →
**Service Tokens** → **Create Service Token**.

| Field | Value |
|---|---|
| Token name | `<service>-bot` (e.g. `alphalens-bot`) |
| Duration | `forever` (or pick an explicit expiry and rotate before it lapses) |

Cloudflare reveals `Client ID` + `Client Secret` **once** — copy both
into a gitignored env file with 0600 perms:

```bash
umask 077 && cat > ~/.secrets/<service>_bot_token.env <<EOF
CF_ACCESS_CLIENT_ID=<client-id>
CF_ACCESS_CLIENT_SECRET=<client-secret>
EOF
```

Attach this token to Policy B (§3) — without an active policy, the token
exists but doesn't grant anything.

---

## 5. Smoke tests

```bash
# A. Unauthorised → 302 redirect to the Access login page
curl -sw '%{http_code} %{redirect_url}\n' -o /dev/null https://<host>/
# expect: 302  https://<team>.cloudflareaccess.com/cdn-cgi/access/login/...

curl -sw '%{http_code}\n' -o /dev/null https://<host>/api/v1/days
# expect: 302

# B. Service Token → 200 (no IdP involved)
set -a && . ~/.secrets/<service>_bot_token.env && set +a
curl -fsS \
    -H "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}" \
    -H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}" \
    https://<host>/api/v1/days | jq '.meta'

# C. Browser: open https://<host>/ in a private window
# → Cloudflare auto-redirects to Google → consent → returns to SPA shell
# → SPA fetches /api/* with the freshly-set Access cookie → data renders
```

If any unauthorised request returns 200, **Access is not in front of the
hostname**. Check that the Application domain matches exactly (no path
suffix), the DNS record is **Proxied** (orange cloud), and that no
policy uses **Bypass** (which disables auth).

---

## 6. CORS

Once production traffic flows via Cloudflare, set `API_CORS_ORIGINS` in
the operator env (`deploy/docker/.env` or wherever the compose
`env_file:` points) so the SPA can call the API from the same origin
without triggering preflight rejections:

```bash
API_CORS_ORIGINS=https://<host>,http://localhost:5173,http://localhost:8085
```

Then bounce the api container:

```bash
UID="$(id -u)" GID="$(id -g)" docker compose \
    -f deploy/docker/docker-compose.yml up -d api
```

---

## 7. Rate limiting (defence in depth)

Cloudflare WAF (Rulesets API, phase `http_ratelimit`) handles per-IP
throttling at the edge. Free tier supports one rule per zone with
period=10s and characteristics=[`ip.src`, `cf.colo.id`]. Sample rule
protecting `/api/*`:

```bash
set -a && . ~/.secrets/cloudflare.env && set +a   # token with Zone WAF:Edit
ZONE=<zone-id>
curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/rulesets" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "alphalens API rate limit",
    "kind": "zone",
    "phase": "http_ratelimit",
    "rules": [{
      "action": "block",
      "description": "/api/* throttle: 50 req/10s per (IP × CF colo)",
      "expression": "(http.host eq \"<host>\" and starts_with(http.request.uri.path, \"/api/\"))",
      "ratelimit": {
        "characteristics": ["ip.src", "cf.colo.id"],
        "period": 10,
        "requests_per_period": 50,
        "mitigation_timeout": 10
      }
    }]
  }'
```

Verify with a burst test against a Service Token endpoint (so the auth
layer doesn't gate the test): N consecutive requests within the window
should yield mostly 200s until the threshold trips, then 429s for the
mitigation window.

---

## 8. Quick setup via API (alternative to GUI clicks)

If you've already created the Google OAuth Client and have a Cloudflare
API token with the right scopes (Account → Access: Apps and Policies +
Service Tokens + Organizations, Identity Providers, and Groups → Edit;
Zone → Zone WAF: Edit), the full Cloudflare-side setup is API-driven.

> **Not idempotent.** Each `POST` below creates a new resource —
> Cloudflare happily accepts duplicate-named Access apps, policies, and
> service tokens. If you re-run a partial sequence after a typo, clean
> up the duplicates in the dashboard first (Zero Trust → Access →
> Applications / Service Auth) or `DELETE` them via API using the IDs
> the earlier `POST`s returned.

```bash
set -a && . ~/.secrets/cloudflare.env && . ~/.secrets/google_oauth.env && set +a
ACCT=<account-id>

# Add Google IdP
GOOGLE_IDP=$(curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/identity_providers" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"Google\",\"type\":\"google\",\"config\":{\"client_id\":\"$GOOGLE_OAUTH_CLIENT_ID\",\"client_secret\":\"$GOOGLE_OAUTH_CLIENT_SECRET\"}}" \
  | jq -r .result.id)

# Create Access app (whole hostname)
APP=$(curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"AlphaLens\",\"domain\":\"<host>\",\"type\":\"self_hosted\",\"session_duration\":\"24h\",\"allowed_idps\":[\"$GOOGLE_IDP\"],\"auto_redirect_to_identity\":true,\"skip_interstitial\":true,\"app_launcher_visible\":false}" \
  | jq -r .result.id)

# Allow-email policy
curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps/$APP/policies" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Allow Kamil","decision":"allow","include":[{"email":{"email":"<your-email>"}}],"precedence":1}'

# Service Token + Service-Auth policy
TOK=$(curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/service_tokens" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"alphalens-bot","duration":"forever"}')
echo "$TOK" | jq -r '"CF_ACCESS_CLIENT_ID=" + .result.client_id, "CF_ACCESS_CLIENT_SECRET=" + .result.client_secret' \
  > ~/.secrets/alphalens_bot_token.env
chmod 600 ~/.secrets/alphalens_bot_token.env
TOK_ID=$(echo "$TOK" | jq -r .result.id)

curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$ACCT/access/apps/$APP/policies" \
  -H "Authorization: Bearer $CLOUDFLARE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"Service Auth: alphalens-bot\",\"decision\":\"non_identity\",\"include\":[{\"service_token\":{\"token_id\":\"$TOK_ID\"}}],\"precedence\":2}"
```

The Google OAuth Client itself still needs the Google Cloud Console GUI
once — the Authorized redirect URI is
`https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`.

---

## 9. Notes / deferred

- **No app-layer JWT verification (yet).** The FastAPI service does not
  validate the `Cf-Access-Jwt-Assertion` header itself — Cloudflare
  blocks unauthorised requests at the edge, and the origin binds to
  `127.0.0.1` so the JWT check inside the app would be duplicate work.
  Adding it later (for audit logging of authenticated emails per
  request) is a follow-up — verify against the app's `aud` value from
  §2.
- **Service Token granularity.** Service Tokens are coarse-grained —
  every bot using the same token has the same access. If a future bot
  needs scoped access (e.g. read-only vs read-stats-only), issue a
  separate token + policy per role rather than splitting at the app.
- **Rotation.** When you rotate a Service Token, update `.env` on every
  host holding the secret **before** revoking the old token (Cloudflare
  has no grace period on revocation).
- **Free tier ceiling.** Cloudflare Access free tier caps at 50 active
  users. If the operator count grows beyond that, switch to a paid
  Teams plan; nothing in this setup presumes a particular SKU.
- **Bind discipline.** Never change the api container's port binding
  from `127.0.0.1:8086:8000` to `0.0.0.0:8086:8000` — the api would then
  answer requests on the VPS's public interface directly, silently
  bypassing every Cloudflare Access policy. Same for `web`'s
  `127.0.0.1:8085`. The threat model assumes the origin is reachable
  **only** via Cloudflare Tunnel; an externally-bound port is equivalent
  to disabling auth.
- **Google OAuth publishing status — publish, do not stay in Testing.**
  A fresh Google Cloud project defaults to `Testing` status, which
  creates a *second* allowlist (OAuth consent screen → Test users) that
  gates SSO independently of Cloudflare Policy A. Two allowlists for the
  same access decision means every new user must be added in both
  places; forgetting one yields a confusing Google-side 403 ("AlphaLens
  has not completed verification") even though Cloudflare would have
  allowed them.
  Click **Publish app** on the OAuth consent screen. With only the
  basic scopes (`openid email profile`) and well under 100 users,
  Google verification is **not** required — publishing is instant and
  removes the Test users gate entirely. Cloudflare Policy A then
  becomes the sole identity allowlist, which matches the threat model
  (Cloudflare Access enforces ZTNA + MFA at the edge; the Google Test
  users list was never load-bearing security, just a development
  guard). Verification is only triggered by sensitive / restricted
  scopes (Gmail, Drive, Calendar — none of which AlphaLens uses) or
  by exceeding the ~100-user cap.
