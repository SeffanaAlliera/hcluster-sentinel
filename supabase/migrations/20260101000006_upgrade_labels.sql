-- =====================================================================
-- UPGRADE for databases created from an EARLIER release (keeps all data: nodes, tokens, history).
-- Safe to run more than once, and a no-op on a fresh install.
--
-- After running this file, RE-RUN 20260101000003_functions.sql (it is create-or-replace, so it is safe)
-- and 20260101000005_process_check.sql. Do NOT re-run 0001 / 0002 (they create objects that already exist).
-- =====================================================================

-- 1) new service check types
alter type public.check_type add value if not exists 'dns';
alter type public.check_type add value if not exists 'process';

-- 2) metrics get a `label` (mountpoint / NIC); existing rows become label '' (node-wide) = unchanged meaning
alter table public.metrics              add column if not exists label text not null default '';
alter table public.node_latest_metrics  add column if not exists label text not null default '';

do $$
begin
  if not exists (select 1 from pg_constraint where conrelid = 'public.metrics'::regclass and conname = 'metrics_label_check') then
    alter table public.metrics add constraint metrics_label_check check (char_length(label) <= 64);
  end if;

  -- primary keys must include label
  if not exists (select 1 from pg_index i join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any (i.indkey)
                  where i.indrelid = 'public.metrics'::regclass and i.indisprimary and a.attname = 'label') then
    alter table public.metrics drop constraint metrics_pkey;
    alter table public.metrics add primary key (node_id, name, label, ts);
  end if;
  if not exists (select 1 from pg_index i join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any (i.indkey)
                  where i.indrelid = 'public.node_latest_metrics'::regclass and i.indisprimary and a.attname = 'label') then
    alter table public.node_latest_metrics drop constraint node_latest_metrics_pkey;
    alter table public.node_latest_metrics add primary key (node_id, name, label);
  end if;

  -- metric_series changed its return type (adds label): drop the old one so 0003 can recreate it
  if exists (select 1 from pg_proc where proname = 'metric_series' and pronamespace = 'public'::regnamespace
              and pg_get_function_result(oid) not like '%label%') then
    drop function public.metric_series(uuid, text[], timestamptz, timestamptz, integer);
  end if;
end $$;
