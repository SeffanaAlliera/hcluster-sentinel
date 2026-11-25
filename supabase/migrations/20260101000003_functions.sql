-- =====================================================================
-- Server-side logic. All functions that mutate data are SECURITY DEFINER and
-- executable ONLY by service_role (the Vercel backend). The browser role can
-- only call metric_series(), which is SECURITY INVOKER (so RLS still applies).
-- =====================================================================

-- ---------- rate limiting ------------------------------------------------
create or replace function public.rate_limit_hit(p_key text, p_limit integer, p_window_seconds integer)
returns boolean
language plpgsql security definer set search_path = public, pg_temp
as $$
declare v_hits integer;
begin
  insert into public.rate_limits as r (key, window_start, hits)
  values (p_key, now(), 1)
  on conflict (key) do update set
    window_start = case when r.window_start < now() - make_interval(secs => p_window_seconds) then now() else r.window_start end,
    hits         = case when r.window_start < now() - make_interval(secs => p_window_seconds) then 1 else r.hits + 1 end
  returning hits into v_hits;
  return v_hits <= p_limit;
end $$;

create or replace function public.rate_limit_blocked(p_key text, p_limit integer, p_window_seconds integer)
returns boolean
language sql stable security definer set search_path = public, pg_temp
as $$
  select exists (select 1 from public.rate_limits
                 where key = p_key and hits >= p_limit
                   and window_start >= now() - make_interval(secs => p_window_seconds))
$$;

-- ---------- node registration / credential rotation ---------------------
create or replace function public.register_node(
  p_hostname text, p_display_name text, p_secret_hash text,
  p_heartbeat_interval integer default 60, p_offline_threshold integer default 180)
returns table (node_id uuid, credential_id uuid)
language plpgsql security definer set search_path = public, pg_temp
as $$
declare v_node uuid; v_cred uuid;
begin
  insert into public.nodes (hostname, display_name, heartbeat_interval_seconds, offline_threshold_seconds)
  values (p_hostname, p_display_name, p_heartbeat_interval, p_offline_threshold)
  returning id into v_node;
  insert into public.node_api_credentials (node_id, secret_hash) values (v_node, p_secret_hash)
  returning id into v_cred;
  return query select v_node, v_cred;
end $$;

create or replace function public.rotate_node_credential(p_node_id uuid, p_secret_hash text)
returns uuid
language plpgsql security definer set search_path = public, pg_temp
as $$
declare v_cred uuid;
begin
  perform 1 from public.nodes where id = p_node_id for update;
  if not found then raise exception 'node_not_found' using errcode = 'P0002'; end if;
  update public.node_api_credentials set revoked_at = now() where node_id = p_node_id and revoked_at is null;
  insert into public.node_api_credentials (node_id, secret_hash) values (p_node_id, p_secret_hash)
  returning id into v_cred;
  return v_cred;
end $$;

-- ---------- status roll-up ----------------------------------------------
-- DEGRADED = heartbeat is fresh but something is wrong (service, Prometheus, metrics).
-- A failing service or Prometheus outage never makes a node OFFLINE.
create or replace function public.recompute_node_status(p_node_id uuid)
returns void
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  v_n public.nodes%rowtype;
  v_bad integer;
  v_reasons text[] := '{}';
begin
  select * into v_n from public.nodes where id = p_node_id;
  if not found or v_n.status = 'offline' then return; end if;
  select count(*) into v_bad from public.services
   where node_id = p_node_id and enabled and status in ('down', 'degraded');
  if v_bad > 0 then v_reasons := v_reasons || format('%s service(s) unhealthy', v_bad); end if;
  if v_n.prometheus_status = 'unreachable' then v_reasons := v_reasons || 'Prometheus unreachable'::text; end if;
  if v_n.metrics_status = 'failed' and v_n.prometheus_status <> 'unreachable' then
    v_reasons := v_reasons || 'Metrics collection failing'::text;
  end if;
  update public.nodes set
    status = case when cardinality(v_reasons) > 0 then 'degraded'::public.node_status else 'online'::public.node_status end,
    status_reason = nullif(array_to_string(v_reasons, '; '), ''),
    updated_at = now()
  where id = p_node_id;
end $$;

-- ---------- ingest -------------------------------------------------------
create or replace function public._ingest_one(p_node_id uuid, p_report jsonb, p_now timestamptz)
returns boolean
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  v_ts          timestamptz := (p_report->>'collected_at')::timestamptz;
  v_latest      boolean;
  v_host        jsonb := coalesce(p_report->'host', '{}'::jsonb);
  v_mr          jsonb := p_report->'metrics_report';
  v_prom        jsonb;
  v_svc         jsonb;
  v_sid         uuid;
  v_name        text;
  v_enabled     boolean;
  v_old_checked timestamptz;
  v_checked     timestamptz;
  v_status      public.health_status;
  v_pstat       text;
  v_mstat       text;
begin
  if v_ts < p_now - interval '24 hours' then return false; end if;   -- too old: drop
  if v_ts > p_now + interval '5 minutes' then v_ts := p_now; end if;  -- clock skew guard

  select (state_ts is null or v_ts >= state_ts) into v_latest from public.nodes where id = p_node_id;

  insert into public.node_heartbeats (node_id, ts, received_at, uptime_seconds, agent_version)
  values (p_node_id, v_ts, p_now, nullif(v_host->>'uptime_seconds', '')::bigint, left(p_report #>> '{agent,version}', 32))
  on conflict (node_id, ts) do nothing;

  if v_latest then
    update public.nodes set
      state_ts       = v_ts,
      os             = coalesce(left(v_host->>'os', 120), os),
      arch           = coalesce(left(v_host->>'arch', 40), arch),
      kernel         = coalesce(left(v_host->>'kernel', 80), kernel),
      agent_version  = coalesce(left(p_report #>> '{agent,version}', 32), agent_version),
      uptime_seconds = coalesce(nullif(v_host->>'uptime_seconds', '')::bigint, uptime_seconds)
    where id = p_node_id;
  end if;

  -- service checks ----------------------------------------------------
  for v_svc in select value from jsonb_array_elements(coalesce(p_report->'services', '[]'::jsonb)) loop
    v_checked := coalesce((v_svc->>'checked_at')::timestamptz, v_ts);
    if v_checked > p_now + interval '5 minutes' then v_checked := p_now; end if;
    v_status := (v_svc->>'status')::public.health_status;

    insert into public.services (node_id, key, name, check_type, target)
    values (p_node_id, v_svc->>'key', left(coalesce(v_svc->>'name', v_svc->>'key'), 80),
            (v_svc->>'type')::public.check_type, left(v_svc->>'target', 300))
    on conflict (node_id, key) do update
      set name = excluded.name, check_type = excluded.check_type, target = excluded.target
    returning id, name, enabled, last_checked_at into v_sid, v_name, v_enabled, v_old_checked;

    insert into public.service_checks (service_id, ts, status, response_ms, http_status, error)
    values (v_sid, v_checked, v_status, nullif(v_svc->>'response_ms', '')::integer,
            nullif(v_svc->>'http_status', '')::integer, left(v_svc->>'error', 500))
    on conflict (service_id, ts) do nothing;

    if v_old_checked is null or v_checked >= v_old_checked then
      update public.services set
        status          = v_status,
        response_ms     = nullif(v_svc->>'response_ms', '')::integer,
        http_status     = nullif(v_svc->>'http_status', '')::integer,
        last_checked_at = v_checked,
        last_success_at = case when v_status in ('operational', 'degraded') then v_checked else last_success_at end,
        last_error      = case when v_status = 'down' then left(v_svc->>'error', 500) else last_error end,
        last_error_at   = case when v_status = 'down' then v_checked else last_error_at end
      where id = v_sid;

      if v_status = 'down' then
        if v_enabled then
          insert into public.incidents (node_id, service_id, kind, title, detail, started_at)
          values (p_node_id, v_sid, 'service_down', 'Service unavailable: ' || v_name, left(v_svc->>'error', 500), v_checked)
          on conflict do nothing;
        end if;
      else
        update public.incidents set resolved_at = greatest(v_checked, started_at), resolved_reason = 'service recovered'
         where service_id = v_sid and kind = 'service_down' and resolved_at is null;
      end if;
    end if;
  end loop;

  -- metrics -----------------------------------------------------------
  if v_mr is not null then
    insert into public.metrics (node_id, name, ts, value, source)
    select p_node_id, e->>'name', v_ts, (e->>'value')::double precision, coalesce(e->>'source', 'prometheus')
      from jsonb_array_elements(coalesce(v_mr->'values', '[]'::jsonb)) e
     where e->>'value' is not null
    on conflict do nothing;

    insert into public.node_latest_metrics as l (node_id, name, value, source, ts)
    select p_node_id, e->>'name', (e->>'value')::double precision, coalesce(e->>'source', 'prometheus'), v_ts
      from jsonb_array_elements(coalesce(v_mr->'values', '[]'::jsonb)) e
     where e->>'value' is not null
    on conflict (node_id, name) do update
      set value = excluded.value, source = excluded.source, ts = excluded.ts
      where excluded.ts >= l.ts;

    if v_latest then
      v_prom  := coalesce(v_mr->'prometheus', '{}'::jsonb);
      v_pstat := coalesce(v_prom->>'status', 'unknown');
      v_mstat := coalesce(v_mr->>'status', 'unknown');

      update public.nodes set
        prometheus_status        = v_pstat,
        prometheus_error         = left(v_prom->>'error', 300),
        prometheus_targets_up    = nullif(v_prom->>'targets_up', '')::integer,
        prometheus_targets_total = nullif(v_prom->>'targets_total', '')::integer,
        prometheus_down_targets  = coalesce(v_prom->'down_targets', '[]'::jsonb),
        metrics_status           = v_mstat,
        metrics_unavailable      = coalesce(v_mr->'unavailable', '[]'::jsonb)
      where id = p_node_id;

      if v_pstat = 'unreachable' then
        insert into public.incidents (node_id, kind, title, detail, started_at)
        values (p_node_id, 'prometheus_unavailable', 'Prometheus unavailable', left(v_prom->>'error', 500), v_ts)
        on conflict do nothing;
      else
        update public.incidents set resolved_at = greatest(v_ts, started_at), resolved_reason = 'Prometheus reachable again'
         where node_id = p_node_id and kind = 'prometheus_unavailable' and resolved_at is null;
      end if;

      if v_mstat = 'failed' and v_pstat <> 'unreachable' then
        insert into public.incidents (node_id, kind, title, detail, started_at)
        values (p_node_id, 'metrics_failure', 'Metrics collection failure', left(v_prom->>'error', 500), v_ts)
        on conflict do nothing;
      else
        update public.incidents set resolved_at = greatest(v_ts, started_at), resolved_reason = 'metrics collection recovered'
         where node_id = p_node_id and kind = 'metrics_failure' and resolved_at is null;
      end if;
    end if;
  end if;

  return true;
end $$;

create or replace function public.ingest_report(p_node_id uuid, p_credential_id uuid, p_reports jsonb)
returns jsonb
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  v_now      timestamptz := now();
  v_node     public.nodes%rowtype;
  v_report   jsonb;
  v_accepted integer := 0;
  v_skipped  integer := 0;
begin
  select * into v_node from public.nodes where id = p_node_id for update;   -- serialise per node
  if not found then raise exception 'node_not_found' using errcode = 'P0002'; end if;
  if not v_node.enabled then raise exception 'node_disabled' using errcode = 'P0001'; end if;

  for v_report in
    select value from jsonb_array_elements(p_reports) with ordinality as t(value, ord)
     order by (value->>'collected_at')::timestamptz, ord
  loop
    if public._ingest_one(p_node_id, v_report, v_now) then v_accepted := v_accepted + 1; else v_skipped := v_skipped + 1; end if;
  end loop;

  update public.nodes set last_heartbeat_at = v_now, updated_at = v_now where id = p_node_id;

  update public.incidents set resolved_at = greatest(v_now, started_at), resolved_reason = 'heartbeat resumed'
   where node_id = p_node_id and kind = 'node_offline' and resolved_at is null;

  -- node was OFFLINE/UNKNOWN: it is talking again, so roll status up from current state
  update public.nodes set status = 'online' where id = p_node_id and status in ('offline', 'unknown');
  perform public.recompute_node_status(p_node_id);

  update public.node_api_credentials set last_used_at = v_now where id = p_credential_id;

  return jsonb_build_object('accepted', v_accepted, 'skipped', v_skipped, 'server_time', v_now);
end $$;

-- ---------- offline detection (called by pg_cron / cron route / dashboard) ---
create or replace function public.evaluate_nodes()
returns integer
language plpgsql security definer set search_path = public, pg_temp
as $$
declare r record; v_count integer := 0;
begin
  for r in
    update public.nodes n set
        status = 'offline',
        status_reason = 'No heartbeat since ' || to_char(n.last_heartbeat_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS "UTC"'),
        updated_at = now()
     where n.enabled and n.status <> 'offline' and n.last_heartbeat_at is not null
       and now() - n.last_heartbeat_at > make_interval(secs => n.offline_threshold_seconds)
    returning n.id, n.last_heartbeat_at
  loop
    insert into public.incidents (node_id, kind, title, detail, started_at)
    values (r.id, 'node_offline', 'Node offline', 'Heartbeat threshold exceeded', r.last_heartbeat_at)
    on conflict do nothing;
    -- service state is unknowable while the node is silent
    update public.services set status = 'unknown' where node_id = r.id;
    v_count := v_count + 1;
  end loop;
  return v_count;
end $$;

-- ---------- retention ---------------------------------------------------
create or replace function public.purge_old_data()
returns jsonb
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  d_metrics   integer := coalesce((select (value #>> '{}')::integer from public.sentinel_settings where key = 'retention_metrics_days'), 14);
  d_checks    integer := coalesce((select (value #>> '{}')::integer from public.sentinel_settings where key = 'retention_service_checks_days'), 14);
  d_hb        integer := coalesce((select (value #>> '{}')::integer from public.sentinel_settings where key = 'retention_heartbeats_days'), 7);
  d_incidents integer := coalesce((select (value #>> '{}')::integer from public.sentinel_settings where key = 'retention_incidents_days'), 365);
  n_m bigint; n_c bigint; n_h bigint; n_i bigint; n_r bigint;
begin
  delete from public.metrics        where ts < now() - make_interval(days => d_metrics);   get diagnostics n_m = row_count;
  delete from public.service_checks where ts < now() - make_interval(days => d_checks);    get diagnostics n_c = row_count;
  delete from public.node_heartbeats where ts < now() - make_interval(days => d_hb);       get diagnostics n_h = row_count;
  delete from public.incidents where resolved_at is not null and resolved_at < now() - make_interval(days => d_incidents); get diagnostics n_i = row_count;
  delete from public.rate_limits where window_start < now() - interval '1 day';            get diagnostics n_r = row_count;
  return jsonb_build_object('metrics', n_m, 'service_checks', n_c, 'heartbeats', n_h, 'incidents', n_i, 'rate_limits', n_r);
end $$;

-- ---------- dashboard time series (RLS applies: SECURITY INVOKER) ----------
create or replace function public.metric_series(
  p_node_id uuid, p_names text[], p_from timestamptz, p_to timestamptz, p_bucket_seconds integer)
returns table (bucket timestamptz, name text, value double precision)
language sql stable security invoker set search_path = public, pg_temp
as $$
  select date_bin(make_interval(secs => greatest(p_bucket_seconds, 10)), m.ts, timestamptz '2000-01-01 00:00:00+00') as bucket,
         m.name, avg(m.value)::double precision
    from public.metrics m
   where m.node_id = p_node_id and m.name = any (p_names) and m.ts >= p_from and m.ts < p_to
   group by 1, 2
   order by 1
$$;

-- ---------- privileges --------------------------------------------------
revoke all on function public.rate_limit_hit(text, integer, integer)               from public, anon, authenticated;
revoke all on function public.rate_limit_blocked(text, integer, integer)           from public, anon, authenticated;
revoke all on function public.register_node(text, text, text, integer, integer)    from public, anon, authenticated;
revoke all on function public.rotate_node_credential(uuid, text)                   from public, anon, authenticated;
revoke all on function public.recompute_node_status(uuid)                          from public, anon, authenticated;
revoke all on function public._ingest_one(uuid, jsonb, timestamptz)                from public, anon, authenticated;
revoke all on function public.ingest_report(uuid, uuid, jsonb)                     from public, anon, authenticated;
revoke all on function public.evaluate_nodes()                                     from public, anon, authenticated;
revoke all on function public.purge_old_data()                                     from public, anon, authenticated;
revoke all on function public.metric_series(uuid, text[], timestamptz, timestamptz, integer) from public, anon;

grant execute on function public.rate_limit_hit(text, integer, integer)            to service_role;
grant execute on function public.rate_limit_blocked(text, integer, integer)        to service_role;
grant execute on function public.register_node(text, text, text, integer, integer) to service_role;
grant execute on function public.rotate_node_credential(uuid, text)                to service_role;
grant execute on function public.recompute_node_status(uuid)                       to service_role;
grant execute on function public._ingest_one(uuid, jsonb, timestamptz)             to service_role;
grant execute on function public.ingest_report(uuid, uuid, jsonb)                  to service_role;
grant execute on function public.evaluate_nodes()                                  to service_role;
grant execute on function public.purge_old_data()                                  to service_role;
grant execute on function public.metric_series(uuid, text[], timestamptz, timestamptz, integer) to authenticated, service_role;
