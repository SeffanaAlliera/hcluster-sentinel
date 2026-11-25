# Security

## Network posture
* The agent **only dials out** (HTTPS to your Vercel URL; it refuses `http://` except localhost for development, and refuses HTTP redirects so the token can never be forwarded elsewhere).
* No port forwarding, Funnel, tunnel or inbound listener is created. The agent opens **no** listening socket. Prometheus, Grafana, Cockpit, SSH stay private on Tailscale.
* Service checks run against `127.0.0.1` on the node. Targets are stored only to show admins what was checked; set `report_target: false` per service to send nothing.

## Secrets
| Secret | Where it lives | Never in |
|---|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | Vercel env (server only) | browser, git, agent |
| `SUPABASE_ANON_KEY`, `SUPABASE_URL` | Vercel env (server only; not `NEXT_PUBLIC_`) | git |
| `CRON_SECRET` | Vercel env | git |
| Node token | `/etc/hcluster-agent/agent.env` (0640 root:hcluster) | `config.yaml`, git, DB (only its SHA-256) |

The browser never talks to Supabase at all: login is a server action and every read happens server-side, so no Supabase key reaches the client.

## Controls
* **Per-node credentials**, 256-bit random, hash-only storage, constant-time compare, revocation + rotation, `last_used_at`.
* **Unauthorized heartbeats**: 401 for any bad/revoked token; identical responses for unknown id vs wrong secret; payload node must equal token's node.
* **Unauthorized registration**: impossible without an admin session; no self-registration endpoint.
* **Dashboard auth**: Supabase Auth (email+password). Authorisation is the `admin_users` table, enforced by RLS — an authenticated stranger sees nothing. Disable public sign-ups (see DEPLOYMENT).
* **RLS**: every table has RLS enabled. `authenticated` has SELECT-only on dashboard tables if `is_admin()`. `node_api_credentials`, `rate_limits`, `sentinel_settings` have no policies and no grants for client roles. All mutating SQL functions are `EXECUTE`-able by `service_role` only (verified by tests).
* **Input validation**: zod schemas with strict enums, regex keys/metric names, numeric bounds, array caps, string truncation, control-char stripping, unknown fields dropped; 256 KB body cap; SQL uses typed casts and parameters (no string-built SQL).
* **Rate limiting** (no Redis): per-instance memory guard → DB-backed limits shared across serverless instances: 30 req/min per node, 20 failed auths / 10 min per IP (then 429 even for valid tokens from that IP). Login: 10 attempts / 10 min / IP per instance + Supabase Auth's own limits. For volumetric abuse, add a Vercel Firewall rate-limit rule on `/api/agent/report`.
* **CSRF**: dashboard mutations require same-origin `Origin` + JSON; cookies are `SameSite=Lax`.
* **Headers**: strict CSP (`connect-src 'self'`), HSTS, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `noindex`.
* **Agent hardening** (systemd): dedicated unprivileged user, `NoNewPrivileges`, `ProtectSystem=strict`, empty capability set, syscall filter, memory/CPU caps.
* `/api/health` reveals nothing; no private IPs or hostnames are exposed to unauthenticated visitors (everything except `/login`, `/api/health`, `/api/agent/*`, `/api/cron/*` requires an admin session; the latter two require a token/secret).

## Residual risks / hardening ideas
* A **stolen node token** lets an attacker submit fake health data for that node only. Rotate it from the node page.
* The in-memory limiter is per serverless instance (best effort); the DB limiter is authoritative. Add Vercel Firewall rules for DDoS-class traffic.
* Consider enabling **MFA** and CAPTCHA in Supabase Auth, and a custom domain behind Vercel's protection features.
* Supabase free projects pause after ~1 week of inactivity; the agents' traffic keeps yours active.
