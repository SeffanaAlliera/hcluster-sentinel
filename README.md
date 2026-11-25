# hCluster Sentinel

External observability dashboard for a homelab behind CGNAT. Agents on your nodes push **outbound HTTPS** to a Vercel-hosted Next.js app backed by Supabase Postgres. Nothing on your home network is exposed; Tailscale stays private. Your existing Prometheus/Grafana are reused, not replaced.

* **Dashboard** (Next.js + Tailwind, dark): overview tiles, node cards (status, last heartbeat, uptime, CPU/RAM/disk, services up), node detail with CPU/memory/disk/network charts (1 h / 6 h / 24 h / 7 d from Supabase), services table (status, response time, last success/error), incident history (outage + recovery), add / disable / remove / rotate-token.
* **Agent** (Python 3, stdlib + PyYAML, systemd): HTTP/TCP/DNS service checks, auto-detected multi-drive usage, CPU temperature, per-NIC + combined network throughput, Prometheus `/api/v1/query` allowlist, `/proc` fallback, offline backlog queue, backoff, watchdog, `discover` command.
* **Database**: normalized schema, RLS, retention, SQL functions for ingest/offline detection/purge/rate limits.
* **Multi-node**: nothing hard-coded to `h1`; add h2/h3 from the UI.

```
agent/      Python agent, config template, systemd unit, install/update/uninstall
web/        Next.js app (Vercel Root Directory = web)
supabase/   migrations (schema, RLS, functions, pg_cron), admin bootstrap, SQL tests
docs/       ARCHITECTURE · DEPLOYMENT · SECURITY · TROUBLESHOOTING
scripts/    test-all.sh
```

**Start here:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) · design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/SECURITY.md](docs/SECURITY.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)

## Tests
`scripts/test-all.sh` runs: SQL tests on the real migrations (PGlite), agent unit tests, web typecheck/unit tests, and an end-to-end test where the **real Python agent** posts to the **real Next.js route handler** backed by the **real SQL** (incl. node failure and recovery).

## Not covered / honest limits
* Verified in a sandbox, not on your h1: service ports, Prometheus URL and exporters must be confirmed with `hcluster-agent discover` (the config ships upstream defaults flagged `VERIFY`).
* Not exercised here: the systemd unit/`useradd` path of `install.sh`, a live Supabase project (RLS was tested on a Postgres-compatible engine), Vercel deployment, and visual rendering of the UI in a browser.
