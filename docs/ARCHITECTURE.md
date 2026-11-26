# Architecture

```
 ┌──────────── your home network (CGNAT, nothing exposed) ───────────┐
 │  h1 / h2 / h3                                                      │
 │   hcluster-agent (Python, systemd)                                 │
 │    ├─ HTTP/TCP checks ─▶ Jellyfin, Immich, Pi-hole, Syncthing, Samba (127.0.0.1)
 │    ├─ /api/v1/query ───▶ Prometheus (existing, optional)           │
 │    └─ /proc fallback metrics (only when Prometheus can't answer)   │
 └──────────────┬─────────────────────────────────────────────────────┘
                │ OUTBOUND HTTPS only, Authorization: Bearer <per-node token>
                ▼
   Vercel: Next.js  /api/agent/report  ── service-role ──▶  Supabase Postgres (RLS on)
                ▲                                              ▲
 Browser ──HTTPS + Supabase-Auth cookie──▶ Next.js pages/APIs ──┘ (reads as the logged-in admin, RLS enforced)
```
Vercel never connects to your network and nothing joins Tailscale; Tailscale remains your private path for SSH/Cockpit/Grafana.

## Status model (four independent layers)
| Layer | Values | Set by |
|---|---|---|
| Node | ONLINE · DEGRADED · OFFLINE · UNKNOWN | heartbeat age + roll-up of the layers below |
| Service | operational · degraded (slow) · down · unknown | agent check results |
| Prometheus | reachable · unreachable · disabled | agent (`/api/v1/targets`) |
| Metrics collection | ok · partial · failed · disabled | agent (per-query results) |

* **OFFLINE** only when no report arrives for `offline_threshold_seconds` (default 180, heartbeat default 60). Services of an offline node show UNKNOWN (they can't be known).
* **DEGRADED** = node is talking but a service is down/slow, Prometheus is unreachable, or metrics collection is failing. A failed service or a Prometheus outage never makes a node OFFLINE.
* *partial* metrics (a metric simply doesn't exist, e.g. no cAdvisor) is shown as "unavailable" and is **not** an incident.

## Offline detection without background processes
`evaluate_nodes()` (SQL) marks stale nodes OFFLINE and opens incidents. It runs from: `pg_cron` every minute (recommended, `migrations/…0004`), `GET /api/cron/evaluate` (Vercel Cron, daily on Hobby plan, plus any external pinger), and lazily whenever an admin loads the dashboard. The UI also derives status from heartbeat age at read time, so it is correct even if none of those ran.

## Prometheus integration
The agent calls Prometheus' HTTP API on the node (`/api/v1/targets` for target up/total, `/api/v1/query` per allowlisted query). Queries live in `config.yaml` (`$SEL` injects optional label matchers for a central Prometheus that scrapes several nodes; each node has its own `base_url`). Only one scalar per metric leaves the node (~10 numbers/min, <3 KB/report). An empty result ⇒ *unavailable*; nothing is estimated. If Prometheus is down the agent keeps heartbeating and checking services, reports `unreachable`, and (optionally) fills the core metrics from `/proc` flagged `source: local`.

## Registration & authentication
1. An admin clicks **Add node** → `register_node()` creates the node + a credential in one transaction.
2. Token = `hcs_<credentialId>_<256-bit secret>`; shown once; only `sha256(secret)` is stored.
3. Each report: look up credential by id → constant-time hash compare → reject revoked/disabled → the **token** determines the node (payload `node_id` must match).
4. No endpoint creates nodes except the admin-only, same-origin-checked `POST /api/nodes`. Rotate/revoke from the node page.

## Metrics collected
Each metric is `(name, label)`; label `''` = node-wide, otherwise a mountpoint or NIC name.
| Metric | Label | Notes |
|---|---|---|
| `cpu_percent`, `cpu_temp_celsius`, `mem_percent`, `mem_available_bytes`, `mem_total_bytes`, `load1`, `load5` | — | CPU temp: node_exporter hwmon, or `/sys` (coretemp/k10temp/thermal_zone) in local fallback; absent (n/a) if no sensor |
| `disk_percent`, `disk_available_bytes`, `disk_total_bytes` | mountpoint | **every** real drive, auto-detected (ext4/xfs/btrfs/zfs/ntfs…; docker/overlay/tmpfs/boot/bind duplicates skipped) |
| `net_rx_bytes_per_sec`, `net_tx_bytes_per_sec` | interface; `''` = **sum of all listed NICs** | physical NICs individually + combined total; virtual NICs (lo, docker, veth, br-, virbr, tailscale) excluded |

Service check types: `http`, `tcp`, `dns` (real UDP DNS query, for resolvers), `process` (process running + bound to given UDP/TCP ports via /proc; e.g. a DHCP-only dnsmasq).

## Data & retention (defaults, table `sentinel_settings`)
metrics 14 d · service_checks 14 d · heartbeats 7 d · resolved incidents 365 d · rate-limit rows 1 d. At 10 metrics/min/node ≈ 14k rows/day/node (~1 MB/day). Purged daily by `purge_old_data()`.
Charts bucket server-side (`metric_series`): 1 h→1 min, 6 h→5 min, 24 h→15 min, 7 d→1 h.

## Report payload (schema 1)
`POST /api/agent/report {schema:1,node_id,reports:[{collected_at,agent,host,services[],metrics_report{status,prometheus{…},values[],unavailable[]}}]}` — up to 20 reports per request (offline backlog replay), 256 KB max. Replays are idempotent; out-of-order reports never overwrite newer state.
