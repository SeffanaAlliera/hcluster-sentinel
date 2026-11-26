# Troubleshooting

**First stop on the node:** `journalctl -u hcluster-agent -n 50 --no-pager` and `sudo hcluster-agent check-config`.
Send one report by hand and see the payload: `sudo hcluster-agent once --send`.

| Symptom | Likely cause / fix |
|---|---|
| Node stays UNKNOWN | No report accepted yet. Check agent logs; `once --send` shows the HTTP error. |
| Log: `authentication refused (HTTP 401)` | Wrong/rotated/revoked token, or typo. Rotate in the dashboard, update `/etc/hcluster-agent/agent.env`, `systemctl restart hcluster-agent`. |
| `HTTP 403 node_disabled` | The node is disabled in the dashboard — enable it. |
| `HTTP 403 node_mismatch` | `node.id` in config.yaml is not the node the token belongs to. |
| `HTTP 429` | Rate limited (agent honours Retry-After). Normal operation sends ~1 request/min; check you didn't set intervals of a few seconds or run two agents on one token. |
| `HTTP 400 invalid_payload` | Version skew or a malformed custom metric name (`^[a-z][a-z0-9_]{0,63}$`). The `issues` field in the response log names the field. |
| `URLError … Name or service not known` | No internet/DNS. The agent queues up to `max_queue` reports and replays them when back. |
| Config error `api.url must be https://` | Use your Vercel HTTPS URL. |
| Service shows **down** but the service works | Wrong port/path in `config.yaml`. Run `sudo hcluster-agent discover`. Docker services: use the published *host* port. HTTPS with self-signed cert: `verify_tls: false`. Pi-hole may redirect/need `/admin/`. |
| Prometheus "Unreachable" | `base_url` wrong, or the container's host mapping isn't on 127.0.0.1 (check `docker ps`/`ss -ltn`). `discover` probes 9091 and 9090. Node stays DEGRADED, not offline. |
| Metric shows "unavailable" | The PromQL returned nothing: exporter missing or label mismatch. Run `discover` (lists metric names + filesystem mountpoints), adjust `expr`/`selector`. Fix mountpoint for disk queries. |
| Metrics "Partial" forever | Some configured queries have no data (e.g. cAdvisor). Remove them or install the exporter. Not an incident. |
| CPU temperature shows n/a | No sensor readable: check `discover` ("CPU temperature sensor"). With Prometheus, node_exporter must expose `node_hwmon_temp_celsius` (adjust the `chip=~` regex to a chip name from `curl :9100/metrics | grep hwmon_chip`). Some VMs/boards have none. |
| A drive is missing | Auto-detection skips non-real filesystems, `/boot`, docker/snap paths and duplicate mounts of one device. Pin drives with `local_metrics.disk_mountpoints`, or adjust the `mountpoint!~` regex in the Prometheus disk queries. node_exporter in Docker: mount `/` with `--path.rootfs`. |
| Only one NIC / wrong NIC listed | `discover` shows physical vs virtual interfaces. Adjust `local_metrics.net_exclude_prefixes` and the `device!~` regex in the net queries. The combined total sums exactly the NICs listed. |
| dnsmasq check down | `type: process` checks the process name (as in `ps -o comm`) is running and bound to the listed ports (DHCP = `listen_udp: [67]`). With `bind-interfaces` it only binds the guest NIC's IP, which is fine. Down means dnsmasq stopped (`systemctl status dnsmasq`). Use `type: dns` only for a real DNS resolver. |
| CPU/net missing for the first minute (local fallback) | Rates need two samples. |
| Central Prometheus, several nodes show the same numbers | Set a distinct `prometheus.selector` per node, e.g. `instance="h2:9100"`. |
| Node never goes OFFLINE | Nothing runs `evaluate_nodes()`. Enable pg_cron (migration 0004) or call `/api/cron/evaluate`; opening the dashboard also triggers it. The UI still *displays* OFFLINE from heartbeat age. |
| Can't log in / "not authorised" | The user must exist in Supabase Auth **and** in `admin_users` (`supabase/bootstrap_admin.sql`). |
| Empty dashboard for a logged-in user | Same cause: RLS shows non-admins nothing. |
| 500 on every page | Missing Vercel env vars (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `CRON_SECRET`); redeploy after adding. |
| Agent restarts every ~3 min | systemd watchdog (180 s) — a check is hanging. Lower `timeout_seconds`/`retries`. |
| `permission denied` writing state | `/var/lib/hcluster-agent` must be owned by `hcluster` (installer does this). |
| Supabase project paused | Free-tier inactivity pause; restore from the Supabase dashboard. |
