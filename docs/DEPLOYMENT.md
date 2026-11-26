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

## 7. Register a node and install the agent (fresh node)
Dashboard → **Add node**: hostname (e.g. `h1`), display name. Copy the **Node ID, API URL and token** (the token is shown once; each node has its own).
On the node (Debian/Ubuntu with systemd, over Tailscale SSH; nothing is exposed, the agent only makes outbound HTTPS calls):
```bash
git clone <your repo> hcluster-sentinel && cd hcluster-sentinel/agent
sudo ./install.sh --api-url https://YOUR-APP.vercel.app --node-id <NODE-UUID>      # prompts for the token (hidden)
# non-interactive:  sudo HCLUSTER_TOKEN=hcs_... ./install.sh --api-url ... --node-id ... --yes
```
What it does, in order: refuses to continue if the node id and token do not belong together (it sends **one real heartbeat** to the API to check; see below) → creates user `hcluster`, `/opt/hcluster-agent` (venv), `/etc/hcluster-agent/{config.yaml,agent.env}`, `/var/lib/hcluster-agent` → **discovers the services running on the machine and adds health checks for them** (section 8) → validates the config → installs and starts `hcluster-agent.service` (enabled on boot). The node turns ONLINE in the dashboard within about a minute, with the discovered services listed under it.

Installer options: `--api-url`, `--node-id`, `--yes` (no prompts), `--reconfigure` (see 7.2), `--no-discover` (skip auto-discovery), `--skip-verify` (do not contact the API; for an offline install), `--strict-verify` (treat "API unreachable" as an error instead of a warning).
Custom locations (rare): set `HCLUSTER_INSTALL_DIR`, `HCLUSTER_CONF_DIR`, `HCLUSTER_STATE_DIR`, `HCLUSTER_UNIT_DIR`, `HCLUSTER_BIN_LINK` for the install; they are recorded in `<install dir>/layout.env`, so `update.sh` and `uninstall.sh` find them again without any environment variables. System directories such as `/etc` or `/var/lib` themselves are refused.

### 7.1 Safe reinstall (re-running the installer)
Running `install.sh` again on a machine that already has an agent is safe:
```bash
sudo ./install.sh                    # repair / refresh: code, venv, unit, wrapper. Identity untouched.
sudo ./install.sh --yes --api-url https://YOUR-APP.vercel.app --node-id <SAME-UUID>   # same values: also fine, nothing is rewritten
```
* An existing `config.yaml` and `agent.env` are **never replaced**. Your node id, API URL, token, services and every other setting stay byte-for-byte identical; the offline queue (`/var/lib/hcluster-agent/queue.json`) is never deleted.
* If you supply a node id, API URL or token that **differs** from the installed one, the installer stops with exit code 3 and changes nothing. It never silently swaps one of them (that is how a node ends up reporting with another node's token).
* Re-running the installer does **not** re-run service discovery (it would otherwise edit a service list you may have curated). Use `hcluster-agent discover-services` for that.
* If the code or config is not valid, the installer aborts and the previous version keeps running (exit code 1).

### 7.2 Reconfigure a node (change node id, token or API URL on purpose)
Only with the explicit flag, and always with the **new node's token**:
```bash
sudo HCLUSTER_TOKEN=hcs_NEW... ./install.sh --reconfigure --node-id <NEW-UUID> --api-url https://YOUR-APP.vercel.app --yes
```
* The new pair is verified against the API first (exit code 4 = token rejected, or the token belongs to a different node; nothing is changed).
* The old `config.yaml` / `agent.env` are backed up next to the originals as `*.bak-<timestamp>` (the 5 newest are kept). Everything else in `config.yaml` (services, intervals, Prometheus) is preserved; only `node.id` / `api.url` are edited in place.
* If the **node id changed**, the existing offline queue belongs to the old node, so it is moved to `queue.node-<first 8 chars of old id>.json.bak` in the state dir. It is not sent to the new node and not deleted.
* Rotating only the token of the same node (dashboard → **Rotate token**) works the same way: `--reconfigure --node-id <same UUID>` with the new token; the queue is kept.

### 7.3 How the node id / token check works
The token is bound to one node on the server. The installer posts a single minimal heartbeat (`agent.version = installer-verify`) with the node id and token and reads the answer: 200 = pair is valid; 401 = the token is not known; 403 `node_mismatch` = the token belongs to a different node (exit 4); 403 `node_disabled` or an unreachable API = warning only (use `--strict-verify` to make unreachable fatal). The token is never printed or logged.

## 8. Service checks and automatic discovery
Each node defines **only its own services** in its own `config.yaml`.

### 8.1 Discovery during installation
On a **fresh** install the installer runs `hcluster-agent discover-services`, which looks at the machine and appends health checks for what it finds to `config.yaml` (inside a clearly marked managed block), before the agent starts. Detection uses several independent signals:
* running systemd units (name and description), e.g. `jellyfin.service`, `ssh.service`, `docker.service`
* listening TCP sockets from `/proc/net/tcp*` and the process that owns each one
* well-known ports, **only as a hint**
* a single unauthenticated `GET` to a local listening port, matched against the application's known response (e.g. Grafana's `/api/health`, Jellyfin's `/health`)

Built in: Jellyfin, Immich, Pi-hole, Syncthing, Samba, SSH, Grafana, Prometheus, Cockpit, Docker, Minecraft servers, Portainer, dnsmasq. The rules are data, not code: `agent/hcluster_agent/signatures.default.yaml`. Add or override applications **without editing it** by creating `/etc/hcluster-agent/signatures.yaml` (same format; an entry with an existing `id` replaces it, a new `id` adds one) or passing `--signatures FILE`.

What it deliberately does **not** do: it never names an application from a port number alone (9090 is Prometheus *and* Cockpit); it ignores ephemeral ports (>= the kernel's `ip_local_port_range`, normally 32768), loopback-only internal endpoints (e.g. a database on 127.0.0.1) unless the application is a known user-facing web UI, and anything inside a Docker network that is not published on the host (it only sees what the host sees). One application gets one check, even if it listens on several ports or on both IPv4 and IPv6. It collects no credentials and makes no authenticated request. https endpoints on the local machine are checked with `verify_tls: false` (self-signed certificates are the norm); change that in the config if you want.

### 8.2 Re-scan later: `hcluster-agent discover-services`
```bash
sudo hcluster-agent discover-services --dry-run     # show what would be added, change nothing
sudo hcluster-agent discover-services               # add what is new, restart the agent if the config changed
```
Options: `--no-restart`, `--include-unidentified`, `--signatures FILE`, `--json`.
* **Safe to repeat.** Run it ten times: the second run says `config unchanged`, no backup is made, no duplicate appears.
* **Never overwrites.** Existing entries are not modified, reordered or removed: custom names, URLs, ports, intervals, retries and `verify_tls` stay exactly as you wrote them. A discovered service counts as *already monitored* if an entry with the same key, **or any entry that already checks the same local port or process**, exists (so your hand-written check under another key is not duplicated).
* **Stale checks are only reported.** If an application is no longer detected (stopped, uninstalled), the command lists its check under "No longer detected". Nothing is deleted from `config.yaml` or from the dashboard.
* **Stable keys.** The key is the signature id (`jellyfin`, `ssh`, ...). Signatures that allow several instances (Minecraft) use `<id>` for the primary port and `<id>-<port>` for others, so a key never changes between runs and the dashboard keeps its history.
* **Safe write.** The config is backed up as `config.yaml.bak-<timestamp>` (5 newest kept), the change is validated (YAML + the full agent config + a check that no existing entry changed) **before** it is written, written atomically (same owner and mode), validated again, and the agent is restarted. If anything fails, including the agent not coming back up, the previous file is restored and the agent restarted on it (exit code 1). A config that is already broken is refused untouched (exit code 2).
* Needs root to write `/etc/hcluster-agent/config.yaml` (use `sudo`).

### 8.3 Unidentified services
Listening ports that no signature recognises are **reported, not added**, e.g.
```
Unidentified TCP endpoints (not added; no application name is guessed): port 6000 (process foo), answers HTTP; port is conventionally used by: ...
```
To monitor one anyway: add it by hand (8.4), or run `discover-services --include-unidentified` which adds a generic check with key `tcp-<port>` and the name "Unidentified TCP :<port>" (HTTP if the port answers HTTP, otherwise plain TCP connect). It is never given an application name.

### 8.4 Add, edit or remove checks manually
Edit `/etc/hcluster-agent/config.yaml` → `services:`; see `agent/config.example.yaml` and `agent/config.h1.example.yaml` for every option. Then `sudo hcluster-agent check-config && sudo systemctl restart hcluster-agent`.
* **Add:** a new item with a unique lowercase `key` (letters, digits, `-_.`). Anything outside the managed block is yours.
* **Edit:** change the entry in place (discovery will not touch it). You can also edit entries inside the managed block; later runs leave them alone.
* **Remove:** delete the item from `config.yaml` and restart. The dashboard keeps the old row (with its history) until you delete it on the node page. If an application is still running, a later `discover-services` run will add it again; to opt out permanently, keep the entry and set `enabled: false` (an existing key is never re-added).
* `sudo hcluster-agent discover` (older command) probes well-known defaults and prints paste-ready YAML; it never writes anything.

Tips: Docker (Immich, Portainer, ...) → the check must use the *published host port* (`docker ps --format '{{.Names}} {{.Ports}}'`); discovery only sees published ports. Syncthing's GUI may listen on 127.0.0.1 only, which is fine, the agent runs on the same host.

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
No code changes. Dashboard → **Add node** → new Node ID + token → run `agent/install.sh` on that machine with **its own** values (services are discovered automatically) → review `sudo hcluster-agent discover-services --dry-run`, adjust `config.yaml` (Prometheus URL/selector) → restart. Disable/remove nodes from the node page; **Rotate token** revokes the old token instantly.

## Updating the agent
```bash
cd hcluster-sentinel && git pull && sudo ./agent/update.sh
```
* Finds the install from `layout.env`, so custom install/config/state/unit paths work without any environment variables (a pre-existing default install without that file is treated as the default layout and gets the file on its first update).
* Never touches `config.yaml`, `agent.env` (token), the offline queue or backups, and does not run service discovery.
* The new code is staged beside the old and **checked against your real config first** (it must import and `check-config` must pass). If not, nothing is swapped and the running agent is untouched (exit code 1, "aborted").
* The virtualenv is rebuilt only when `requirements.txt` changed, in a separate directory that replaces the old one only after the check passes.
* The systemd unit is re-rendered with **your** paths. If the service does not come back up, the previous code, virtualenv and unit are restored and the service is restarted on them (exit code 1).
* The previous code is kept as `<install dir>/app.prev` for manual recovery. After updating, review `agent/config.example.yaml` for new optional settings.
* If the service is not running/enabled, the update does not start it; it tells you.
* `git pull` refusing because of a local mode change on the script: `git checkout -- agent/` or run `sudo bash ./agent/update.sh`.

## Removing the agent
```bash
sudo ./agent/uninstall.sh            # remove service, code and the hcluster-agent command; KEEP config, token, backups and queue
sudo ./agent/uninstall.sh --purge    # also delete /etc/hcluster-agent, /var/lib/hcluster-agent and the hcluster user
```
Without `--purge` a later `install.sh` on the same machine finds the node's config and token and simply reinstalls it (same identity). With `--purge` everything of the node is gone and you install it like a fresh node (create a new token in the dashboard if the old one is lost). Removing the agent does not delete the node from the dashboard; disable or delete it on the node page.

## Recovery
| Situation | What to do |
|---|---|
| Installer says "differs from the installed ..." (exit 3) | You passed another node id/URL/token than the installed one. Run it without those options to refresh safely, or add `--reconfigure` if you really mean to change identity (7.2). |
| "token is for a DIFFERENT node" / "rejected" (exit 4) | Wrong token for that node id. In the dashboard, open the node and use **Rotate token** to get a new one, then `--reconfigure` with it. |
| Update or install aborted (exit 1) | Nothing was changed; the running agent is untouched. Read the message (usually a `config.yaml` error with line numbers), fix it with `sudo hcluster-agent check-config`, run again. |
| Service did not start after an update | The updater already restored the previous version. `journalctl -u hcluster-agent -n 50` for the reason. Manual fallback: `app.prev` in the install dir. |
| `config.yaml` was damaged | Newest backup: `ls -t /etc/hcluster-agent/config.yaml.bak-*`, copy it back, `sudo hcluster-agent check-config`, restart. Same for `agent.env.bak-*`. |
| Wrong node/token saved after a manual edit | `sudo python3 -m hcluster_agent.ident show --config /etc/hcluster-agent/config.yaml --env /etc/hcluster-agent/agent.env` (run from `agent/`) prints the node id, the token's credential id and whether the token looks valid, without revealing the token. |
| Offline queue after changing the node id | `/var/lib/hcluster-agent/queue.node-<old id prefix>.json.bak`; delete it if the old node's backlog is not needed. |
| Another install/update already running (exit 6) | Wait for it; the lock is `<install dir>.lock` and is released automatically. |
| Lost the machine / start over | `uninstall.sh --purge`, create a new token, fresh install. |

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
   `update.sh` replaces only the code and restarts the service (rolling back on failure, see *Updating the agent*); your `/etc/hcluster-agent/config.yaml`, token and queue are untouched. If a release adds new config options (e.g. a new service type), compare with `agent/config.example.yaml` and edit your config, then `sudo systemctl restart hcluster-agent`.

### Upgrading a database you already filled with data (nodes, API tokens, history)
If you applied 0001–0003 from an **earlier zip**, do **not** reset anything. Your nodes, token hashes and history are kept, and agents keep using their existing tokens. In the Supabase SQL Editor, run these files **in this order**, each as its own query (skip any you have already run, except `0003`, which you re-run every time):

1. `supabase/migrations/20260101000006_upgrade_labels.sql` (adds `dns`/`process` check types and the metric `label` column; idempotent)
2. `supabase/migrations/20260101000007_host_cpu.sql` (adds CPU model / thread count columns shown on the node page)
3. `supabase/migrations/20260101000008_node_timezone.sql` (stores each node's timezone so the dashboard shows node-local time)
4. `supabase/migrations/20260101000003_functions.sql` (create-or-replace, safe to re-run)
5. `supabase/migrations/20260101000005_process_check.sql`

Do **not** re-run 0001 or 0002. Existing metric rows become "node-wide" (empty label). Then push the web code, run `git pull && sudo ./agent/update.sh` on each node, and add any new services (e.g. `dnsmasq-dhcp`) to the node's config. `supabase/tests/upgrade.mjs` tests this path against a copy of the old schema.

Order when a release changes both sides: **database → web → agents** (new agents may send fields old servers reject; old agents always work with new servers).
