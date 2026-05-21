# Cloudflare Access for the AlphaLens briefs API

This is an operator runbook for fronting the FastAPI service at
`127.0.0.1:8081` (the `alphalens-api` container from
`deploy/docker/docker-compose.yml`) with **Cloudflare Tunnel + Access**.

Why both:
- **Tunnel** maps a public hostname onto a private origin (the API binds
  to localhost on the VPS, so no port is exposed to the public internet).
- **Access** is the auth layer — Google SSO for browser users, Service
  Tokens for bots/scripts. The API itself stays auth-free; Cloudflare
  validates the JWT at the edge before forwarding.

Free Zero Trust tier (up to 50 users) covers everything below.

---

## 1. Tunnel ingress

If you already run a `cloudflared` tunnel on the VPS (for the web app),
add an ingress rule for the API hostname. Edit the tunnel config (commonly
`~/.cloudflared/config.yml` or `/etc/cloudflared/config.yml`):

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/jacoren/.cloudflared/<your-tunnel-id>.json

ingress:
  # Existing web app
  - hostname: briefs.example.com
    service: http://127.0.0.1:8080
  # NEW: api
  - hostname: api.briefs.example.com
    service: http://127.0.0.1:8081
  - service: http_status:404
```

Apply:

```bash
sudo systemctl restart cloudflared   # OR `cloudflared tunnel run` if attached
cloudflared tunnel info <your-tunnel-id>
```

Add the public DNS record (Cloudflare dashboard → DNS):

```
api.briefs.example.com  CNAME  <tunnel-id>.cfargotunnel.com  (Proxied)
```

---

## 2. Access application + policy

Cloudflare dashboard → **Zero Trust** → **Access** → **Applications** →
**Add an application** → **Self-hosted**.

- **Application name**: `AlphaLens Briefs API`
- **Session duration**: 24 hours (browser cookie lifetime; bots use Service
  Tokens and ignore this)
- **Application domain**: `api.briefs.example.com`
- **Identity providers**: enable **Google** (or whichever IdP you've
  configured under Zero Trust → Settings → Authentication)

Then **Add a policy** (one per role):

### Policy A — operator browser SSO

| Field | Value |
|---|---|
| Policy name | `operator-google-sso` |
| Action | **Allow** |
| Include | Emails → `pajakkamil@gmail.com` |
| Require | (nothing — single user) |

### Policy B — service tokens (bots / scripts)

| Field | Value |
|---|---|
| Policy name | `service-tokens` |
| Action | **Allow** (Service Auth) |
| Include | Service Token → (pick the token created in step 3) |

Save. The browser policy short-circuits the SSO flow for operator users;
the service-token policy lets any caller that presents matching
`CF-Access-Client-Id` + `CF-Access-Client-Secret` headers through.

---

## 3. Service Token issuance

Cloudflare dashboard → **Zero Trust** → **Access** → **Service Auth** →
**Service Tokens** → **Create Service Token**.

- **Token name**: `alphalens-bot-token` (any human-readable name)
- **Duration**: pick a long expiry (e.g. 1 year); rotate before it lapses

Cloudflare reveals `Client ID` + `Client Secret` **once**. Copy both into
the operator `.env` (gitignored — never commit):

```bash
# on the VPS, alongside deploy/docker/.env or wherever your bot reads from
CF_ACCESS_CLIENT_ID=...        # ends in .access
CF_ACCESS_CLIENT_SECRET=...    # 64-hex string
```

Attach this token to the **service-tokens** policy from step 2 (Include →
Service Token → `alphalens-bot-token`).

---

## 4. Smoke tests

```bash
# Unauthorised → 302 redirect to the Access login page
curl -sw "%{http_code} %{redirect_url}\n" -o /dev/null \
    https://api.briefs.example.com/v1/days
# → 302  https://<your-team>.cloudflareaccess.com/cdn-cgi/access/login/...

# Authorised via Service Token → 200
curl -fsS \
    -H "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}" \
    -H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}" \
    https://api.briefs.example.com/v1/days | jq '.meta'

# Browser: open https://api.briefs.example.com/docs in a private window
# → Cloudflare login screen → pick Google → land on Swagger UI
```

If the unauthorised request returns 200, **Access is not in front of the
hostname**. Check that the Application domain matches exactly (including
subdomain) and that the policy action is **Allow** (not **Bypass**, which
disables auth).

---

## 5. Production CORS

Once the api is reachable at `https://api.briefs.example.com`, also let
the web app's public origin call it. Set `API_CORS_ORIGINS` in the
compose `.env` (consumed by `deploy/docker/docker-compose.yml` → `api`
service):

```bash
API_CORS_ORIGINS=https://briefs.example.com,http://localhost:5173,http://localhost:8080
```

Restart the api container so the new value lands:

```bash
UID="$(id -u)" GID="$(id -g)" docker compose \
    -f deploy/docker/docker-compose.yml up -d api
```

---

## 6. Notes / deferred

- The FastAPI service does **not** validate the `Cf-Access-Jwt-Assertion`
  header itself — Cloudflare blocks unauthorised requests at the edge,
  and the origin only binds to `127.0.0.1`, so the JWT check inside the
  app would be duplicate work. Adding it later (for audit logging of
  authenticated emails) is a follow-up issue.
- Service Tokens are coarse-grained — every bot using the same token has
  the same access. If a future bot needs scoped access (e.g. read-only
  vs read-stats-only), issue a separate token + policy per role rather
  than splitting at the application layer.
- Service Token rotation: when you rotate, update `.env` on every host
  that holds the secret **before** revoking the old token (Cloudflare has
  no grace period on revocation).
- Cloudflare Access free tier caps at 50 active users. If the operator
  count grows, switch to a paid Teams plan; nothing in this setup
  presumes a particular SKU.
