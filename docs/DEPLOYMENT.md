# Deployment guide

Time needed: ~30 min. Everything is free-tier friendly (Supabase Free, Vercel Hobby).

## 1. Create the Supabase project
supabase.com → New project (pick a region near you, save the DB password). Then **Project Settings → API**: note the *Project URL*, the *anon* key and the *service_role* key (secret!).

## 2. Apply the SQL schema
Supabase **SQL Editor**, run in order (paste each file, Run):
1. `supabase/migrations/20260101000001_schema.sql`
2. `supabase/migrations/20260101000002_rls.sql`
3. `supabase/migrations/20260101000003_functions.sql`
4. `supabase/migrations/20260101000004_cron_optional.sql` — recommended: enables `pg_cron` (offline detection every minute + nightly retention). If your plan/region doesn't allow it, skip it (see ARCHITECTURE → offline detection).

(With the Supabase CLI instead: `supabase link && supabase db push`.)

## 3. Authentication & RLS
RLS is already on for every table (migration 2). Now create your admin:
1. **Authentication → Providers → Email**: keep enabled. **Authentication → Sign In / Providers → turn OFF "Allow new users to sign up"** (and optionally "Confirm email" off for a single private user).
2. **Authentication → Users → Add user** (your email + a strong password).
3. SQL Editor: edit the e-mail in `supabase/bootstrap_admin.sql` and run it. Only users in `admin_users` can read data.
4. Optional: enable MFA / CAPTCHA in Auth settings.

## 4. Create the Vercel project
Push this repo to GitHub. Vercel → **Add New → Project** → import it → **Root Directory: `web`** (framework auto-detected as Next.js).

## 5. Environment variables (Vercel → Settings → Environment Variables, Production)
| Name | Value |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_ANON_KEY` | anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key (**secret**; do not mark as public) |
| `CRON_SECRET` | `openssl rand -hex 32` (≥ 32 chars) |

## 6. Deploy
Click Deploy (or push to `main`). Open the URL → you should see the login page. Sign in with your admin user → empty dashboard. `GET /api/health` → `{"ok":true}`.
Vercel Cron (`web/vercel.json`) calls `/api/cron/evaluate` daily (Hobby limit); the real 1-minute schedule is pg_cron from step 2.

## 7. Register h1 and install the agent
Dashboard → **Add node**: hostname `h1`, display name e.g. "Home server". Copy the **Node ID, API URL and token** (shown once).
On h1 (Ubuntu), over Tailscale SSH:
```bash
git clone <your repo> hcluster-sentinel && cd hcluster-sentinel/agent
sudo ./install.sh --api-url https://YOUR-APP.vercel.app --node-id <NODE-UUID>     # prompts for the token (hidden)
```
It creates user `hcluster`, `/opt/hcluster-agent` (venv), `/etc/hcluster-agent/{config.yaml,agent.env}`, installs and starts `hcluster-agent.service` (enabled on boot, `Restart=always`).

## 8. Configure service checks — verify real ports first
Each node defines **only its own services** in its `config.yaml` (new nodes start with an empty list). For h1, copy the `services:` block from `agent/config.h1.example.yaml`; for h2/h3 add whatever they actually run.
```bash
sudo hcluster-agent discover
```
It lists the drives it will monitor, the CPU temperature sensor, network interfaces (physical vs virtual), listening TCP ports (with process names), probes the well-known defaults for Jellyfin (8096), Immich (2283), Pi-hole (80/8080), Syncthing (8384), Samba (445), Portainer (9000/9443), GPTWOL (5000) and a DNS query to 127.0.0.1:53 for dnsmasq and prints a ✔/✘ per endpoint plus paste-ready YAML for those that answered. Edit `/etc/hcluster-agent/config.yaml` → `services:` accordingly (the values in `config.h1.example.yaml` are **upstream defaults to verify**, not facts about your machine), then `sudo systemctl restart hcluster-agent`.
Tips: Docker (Immich) → use the *published host port*; `docker ps --format '{{.Names}} {{.Ports}}'`. Syncthing GUI may listen on 127.0.0.1 only — fine, the agent runs on the same host.

## 9. Connect the agent to Prometheus
`discover` also probes `http://127.0.0.1:9091` (your host mapping) then `:9090`, lists targets and which node_exporter/cAdvisor metrics exist, and shows filesystem mountpoints. Then in `config.yaml`:
* `prometheus.base_url` — the URL `discover` confirmed.
* `prometheus.queries` — keep the ones whose metrics are ✔ present; fix `mountpoint="/"` if needed. Unavailable metrics are reported as such, never estimated.
* Central Prometheus scraping several nodes → set `selector: 'instance="h2:9100"'` per node. Independent Prometheus per node → just give each node's agent its own `base_url`.
* No Prometheus on a node → `prometheus.enabled: false`; core metrics come from `/proc`.

## 10. Test heartbeat reporting
```bash
sudo hcluster-agent once --send       # prints the payload; "queued after send: 0" = delivered
journalctl -u hcluster-agent -f
```
Dashboard: h1 turns ONLINE (or DEGRADED if a service/Prometheus is failing) within a minute; "Last heartbeat" ticks.

## 11. Test service availability
`sudo systemctl stop jellyfin` (or any service) → within ~1–2 min its row turns **Down**, node → **Degraded** (not offline), an incident "Service unavailable" opens. `sudo systemctl start jellyfin` → recovers, incident resolves.

## 12. Test Prometheus metrics collection
Charts fill after a few minutes (Last hour tab first). Node page → "Availability layers" shows Prometheus Reachable, targets up/total, metrics collection OK/Partial. Stop it to test: `docker stop <prometheus container>` → Prometheus "Unreachable", node Degraded, heartbeats and service checks continue, incident "Prometheus unavailable"; core metrics continue from `/proc` (source local). Start it again → recovers.

## 13. Simulate node failure
```bash
sudo systemctl stop hcluster-agent         # or: sudo ip link set <iface> down / unplug network
```
After `offline_threshold_seconds` (180 s default) + ≤1 min: node **Offline**, services Unknown, incident "Node offline" (started at the last heartbeat). `sudo systemctl start hcluster-agent` → node recovers, incident resolved. Network-only outage: the agent queues reports (up to 12 h) and back-fills history when connectivity returns.

## 14. Register h2 and h3
No code changes. Dashboard → **Add node** → new Node ID + token → run `agent/install.sh` on that machine with its own values → edit its `config.yaml` (its own services, Prometheus URL/selector) → restart. Disable/remove nodes from the node page; **Rotate token** revokes the old token instantly.

## Updating / removing the agent
```bash
cd hcluster-sentinel && git pull && sudo ./agent/update.sh      # code only; config and token untouched
sudo ./agent/uninstall.sh [--purge]                              # --purge also deletes config, token, state, user
```
Updating the web app = `git push` (Vercel redeploys). New SQL migrations: run the new file in the SQL editor.

## Local development
```bash
cd web && cp .env.example .env.local   # fill in, then: npm install && npm run dev
scripts/test-all.sh                    # all automated tests
```

## How to update (after you receive new code)
Updates have three independent parts. Do only the parts that changed (the release notes / my message will say which).

1. **Put the new code in your repo** (on the PC where your clone lives):
   ```bash
   cd hcluster-sentinel
   unzip -o ~/Downloads/hcluster-sentinel.zip -d /tmp/hcs && cp -r /tmp/hcs/hcluster-sentinel/. .
   git status && git diff --stat       # review what changed
   git add -A && git commit -m "Update Sentinel" && git push
   ```
2. **Web app:** `git push` is enough. Vercel rebuilds and deploys automatically (watch Deployments; env vars are kept).
3. **Database:** only if there is a NEW file in `supabase/migrations/` (higher number than you have applied). Open the Supabase SQL Editor and run just the new file(s), in order. Never re-run old ones. Migrations are additive and safe to apply with the dashboard live.
4. **Each node's agent:** on h1 (and h2/h3), over Tailscale SSH:
   ```bash
   cd ~/hcluster-sentinel && git pull && sudo ./agent/update.sh
   ```
   `update.sh` replaces only the code and restarts the service; your `/etc/hcluster-agent/config.yaml` and token are untouched. If a release adds new config options (e.g. a new service type), compare with `agent/config.example.yaml` and edit your config, then `sudo systemctl restart hcluster-agent`.

### Upgrading a database you already filled with data (nodes, API tokens, history)
If you applied 0001–0003 from an **earlier zip**, do **not** reset anything. Your nodes, token hashes and history are kept, and agents keep using their existing tokens. In the Supabase SQL Editor, run these three files **in this order**, each as its own query:

1. `supabase/migrations/20260101000006_upgrade_labels.sql` (adds `dns`/`process` check types and the metric `label` column; idempotent)
2. `supabase/migrations/20260101000003_functions.sql` (create-or-replace, safe to re-run)
3. `supabase/migrations/20260101000005_process_check.sql`

Do **not** re-run 0001 or 0002. Existing metric rows become "node-wide" (empty label). Then push the web code, run `git pull && sudo ./agent/update.sh` on each node, and add any new services (e.g. `dnsmasq-dhcp`) to the node's config. `supabase/tests/upgrade.mjs` tests this path against a copy of the old schema.

Order when a release changes both sides: **database → web → agents** (new agents may send fields old servers reject; old agents always work with new servers).
