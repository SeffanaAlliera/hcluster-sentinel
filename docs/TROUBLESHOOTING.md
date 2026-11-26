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

## Only `/` (and `/boot/efi`) shows up as drives, or CPU temperature is missing
Run `sudo hcluster-agent discover` on the node. It lists the drives the agent will monitor, **every temperature sensor the kernel exposes**, and the NICs.
- Drives: the agent adds any real mounted drive that Prometheus does not report (e.g. node_exporter in Docker only sees its own mounts) and hides `/boot/*`. Unmounted drives can't be measured: mount them first (`lsblk -f`, `/etc/fstab`).
- Temperature: if `discover` shows no sensor, load the driver (`sudo modprobe coretemp`, and add `coretemp` to `/etc/modules` to persist). If it shows a sensor under another chip name, tell me the name. If it shows one but the dashboard doesn't, the agent is still the old version: `sudo ./agent/update.sh`.
- Network: one overall throughput metric (all NICs summed). Set `local_metrics.net_per_interface: true` to also see each NIC.

## GPU charts
- No GPU charts at all: the node has no supported GPU, or `local_metrics.gpu: false`. `sudo hcluster-agent discover` has a "GPU" section showing what was found and what could be read.
- Intel GPU shows a clock but no utilisation, and "gpu_utilization_percent" is listed as unavailable: the agent may not read the i915 performance counters. Install the opt-in drop-in (see `agent/systemd/gpu-intel.conf`); if it is still denied, check `cat /proc/sys/kernel/perf_event_paranoid` (Ubuntu defaults to 4; CAP_PERFMON overrides it).
- Intel GPU clock reads 0 MHz when idle: the GPU is in its power-saving state. It rises under load (for example during hardware transcoding).
- No VRAM chart on Intel: integrated GPUs share system RAM and expose no VRAM counter. The chart appears for AMD/NVIDIA cards.
- NVIDIA shows nothing: needs `nvidia-smi` on the PATH and `PrivateDevices=no` (see the drop-in file comments).

## Service discovery found nothing / the wrong thing
Run `sudo hcluster-agent discover-services --dry-run` and read every line, including `skipped:` and the unidentified list.
* **Nothing detected, but the app is running:** the signature needs strong evidence (a running systemd unit, the owning process, or the HTTP answer). Docker containers have no host unit; their port must be *published* on the host and answer like the app. Check with `ss -ltnp` and `curl -s http://127.0.0.1:<port>/<path>`. If your install differs (another port or process name), add an entry to `/etc/hcluster-agent/signatures.yaml`.
* **"unit is running but no listening port was found":** the service listens on a port that is not one of its known ports and its process is not recognised; add it by hand or with a signature.
* **"loopback-only ... not added":** the endpoint is only on 127.0.0.1 and is not a known web UI. Add a manual check if you want it.
* **"cannot write ... (run with sudo)"** → run with `sudo`. **"nothing was changed" with exit 2** → `config.yaml` is already invalid: `sudo hcluster-agent check-config` shows the line.
* **"rolled back to the previous config.yaml"** → the agent did not restart with the new checks; the old file is back and running. See `journalctl -u hcluster-agent`; the attempted file is not kept, the backup `config.yaml.bak-*` is the old one.
* **A discovered check shows "down":** the check uses the address the port is bound to; verify with the same `curl`. For https add/keep `verify_tls: false`.
* Installer printed "automatic discovery failed": the install itself succeeded; run `sudo hcluster-agent discover-services` afterwards to see the error.
