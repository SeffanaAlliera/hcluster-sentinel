-- =====================================================================
-- hCluster Sentinel — schema (tables, types, indexes)
-- Run order: 0001 schema -> 0002 rls -> 0003 functions -> (optional) 0004 cron
-- All timestamps are timestamptz (stored and compared in UTC).
-- =====================================================================

create type public.node_status    as enum ('online', 'offline', 'degraded', 'unknown');
create type public.health_status  as enum ('operational', 'degraded', 'down', 'unknown');
create type public.check_type     as enum ('http', 'tcp', 'dns');
create type public.incident_kind  as enum ('node_offline', 'service_down', 'prometheus_unavailable', 'metrics_failure');

-- Dashboard administrators. Rows reference Supabase Auth users.
create table public.admin_users (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz not null default now()
);

-- Registered nodes (h1, h2, h3, ...). Nothing here is hard-coded to a hostname.
create table public.nodes (
  id                         uuid primary key default gen_random_uuid(),
  hostname                   text not null check (char_length(hostname) between 1 and 253),
  display_name               text not null check (char_length(display_name) between 1 and 80),
  os                         text,
  arch                       text,
  kernel                     text,
  agent_version              text,
  enabled                    boolean not null default true,
  status                     public.node_status not null default 'unknown',
  status_reason              text,
  heartbeat_interval_seconds integer not null default 60  check (heartbeat_interval_seconds between 5 and 3600),
  offline_threshold_seconds  integer not null default 180 check (offline_threshold_seconds between 10 and 86400),
  last_heartbeat_at          timestamptz,           -- server clock, last accepted report
  state_ts                   timestamptz,           -- agent clock of newest report applied to "current state"
  uptime_seconds             bigint,
  -- Prometheus connectivity and metrics collection are tracked separately from node availability
  prometheus_status          text not null default 'unknown' check (prometheus_status in ('unknown','ok','unreachable','disabled')),
  prometheus_error           text,
  prometheus_targets_up      integer,
  prometheus_targets_total   integer,
  prometheus_down_targets    jsonb not null default '[]'::jsonb,
  metrics_status             text not null default 'unknown' check (metrics_status in ('unknown','ok','partial','failed','disabled')),
  metrics_unavailable        jsonb not null default '[]'::jsonb,
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now(),
  constraint nodes_threshold_gt_interval check (offline_threshold_seconds > heartbeat_interval_seconds)
);
create index nodes_status_idx on public.nodes (status);

-- One or more API credentials per node. Only a SHA-256 hash of the secret is stored.
create table public.node_api_credentials (
  id          uuid primary key default gen_random_uuid(),
  node_id     uuid not null references public.nodes (id) on delete cascade,
  secret_hash text not null,
  created_at  timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at  timestamptz
);
create index node_api_credentials_node_idx on public.node_api_credentials (node_id);

create table public.node_heartbeats (
  id             bigint generated always as identity primary key,
  node_id        uuid not null references public.nodes (id) on delete cascade,
  ts             timestamptz not null,          -- agent-reported collection time (clamped)
  received_at    timestamptz not null default now(),
  uptime_seconds bigint,
  agent_version  text,
  unique (node_id, ts)
);
create index node_heartbeats_ts_idx on public.node_heartbeats (ts);

create table public.services (
  id              uuid primary key default gen_random_uuid(),
  node_id         uuid not null references public.nodes (id) on delete cascade,
  key             text not null check (key ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  name            text not null,
  check_type      public.check_type not null,
  target          text,                         -- display only; visible to admins only (RLS)
  enabled         boolean not null default true, -- false = muted (no incidents, ignored in node rollup)
  status          public.health_status not null default 'unknown',
  response_ms     integer,
  http_status     integer,
  last_checked_at timestamptz,
  last_success_at timestamptz,
  last_error      text,
  last_error_at   timestamptz,
  created_at      timestamptz not null default now(),
  unique (node_id, key)
);

create table public.service_checks (
  id          bigint generated always as identity primary key,
  service_id  uuid not null references public.services (id) on delete cascade,
  ts          timestamptz not null,
  status      public.health_status not null,
  response_ms integer,
  http_status integer,
  error       text,
  unique (service_id, ts)
);
create index service_checks_ts_idx on public.service_checks (ts);

-- Narrow time series: one row per (node, metric, time). Names are canonical, e.g. cpu_percent.
create table public.metrics (
  node_id uuid not null references public.nodes (id) on delete cascade,
  name    text not null check (name ~ '^[a-z][a-z0-9_]{0,63}$'),
  label   text not null default '' check (char_length(label) <= 64),   -- '' = node-wide; else mountpoint / interface name
  ts      timestamptz not null,
  value   double precision not null,
  source  text not null default 'prometheus' check (source in ('prometheus','local')),
  primary key (node_id, name, label, ts)
);
create index metrics_ts_idx on public.metrics (ts);

-- Latest value per node+metric so node cards never scan the time series.
create table public.node_latest_metrics (
  node_id uuid not null references public.nodes (id) on delete cascade,
  name    text not null,
  label   text not null default '',
  value   double precision not null,
  source  text not null,
  ts      timestamptz not null,
  primary key (node_id, name, label)
);

create table public.incidents (
  id              uuid primary key default gen_random_uuid(),
  node_id         uuid not null references public.nodes (id) on delete cascade,
  service_id      uuid references public.services (id) on delete cascade,
  kind            public.incident_kind not null,
  title           text not null,
  detail          text,
  started_at      timestamptz not null,
  resolved_at     timestamptz,
  resolved_reason text,
  check (resolved_at is null or resolved_at >= started_at)
);
-- At most one OPEN incident per (node, kind, service)
create unique index incidents_one_open_idx
  on public.incidents (node_id, kind, coalesce(service_id, '00000000-0000-0000-0000-000000000000'::uuid))
  where resolved_at is null;
create index incidents_started_idx      on public.incidents (started_at desc);
create index incidents_node_started_idx on public.incidents (node_id, started_at desc);
create index incidents_open_idx         on public.incidents (node_id) where resolved_at is null;

-- Tunables (retention etc.). Values are JSON.
create table public.sentinel_settings (
  key   text primary key,
  value jsonb not null
);
insert into public.sentinel_settings (key, value) values
  ('retention_metrics_days',        '14'),
  ('retention_service_checks_days', '14'),
  ('retention_heartbeats_days',     '7'),
  ('retention_incidents_days',      '365');

-- Cross-instance rate limiting without Redis (serverless-safe)
create table public.rate_limits (
  key          text primary key,
  window_start timestamptz not null default now(),
  hits         integer not null default 0
);
