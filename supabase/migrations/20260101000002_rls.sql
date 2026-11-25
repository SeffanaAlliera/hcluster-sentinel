-- =====================================================================
-- Row Level Security + grants
-- Model: the browser/dashboard session (role "authenticated") may only READ,
-- and only if the user is listed in admin_users. All writes and all node-token
-- handling happen server-side with the service-role key (never sent to browsers).
-- =====================================================================

-- Start from zero privileges for client roles (Supabase grants broadly by default).
revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke execute on functions from public, anon, authenticated;

create or replace function public.is_admin()
returns boolean
language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from public.admin_users where user_id = auth.uid()) $$;
revoke all on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated, service_role;

alter table public.admin_users          enable row level security;
alter table public.nodes                enable row level security;
alter table public.node_api_credentials enable row level security;
alter table public.node_heartbeats      enable row level security;
alter table public.services             enable row level security;
alter table public.service_checks       enable row level security;
alter table public.metrics              enable row level security;
alter table public.node_latest_metrics  enable row level security;
alter table public.incidents            enable row level security;
alter table public.sentinel_settings    enable row level security;
alter table public.rate_limits          enable row level security;

-- Admins can read their own admin row (used by the app to confirm access).
create policy admin_users_self on public.admin_users
  for select to authenticated using (user_id = (select auth.uid()));

-- Read-only policies for admins on dashboard data.
create policy nodes_admin_read               on public.nodes               for select to authenticated using ((select public.is_admin()));
create policy node_heartbeats_admin_read     on public.node_heartbeats     for select to authenticated using ((select public.is_admin()));
create policy services_admin_read            on public.services            for select to authenticated using ((select public.is_admin()));
create policy service_checks_admin_read      on public.service_checks      for select to authenticated using ((select public.is_admin()));
create policy metrics_admin_read             on public.metrics             for select to authenticated using ((select public.is_admin()));
create policy node_latest_metrics_admin_read on public.node_latest_metrics for select to authenticated using ((select public.is_admin()));
create policy incidents_admin_read           on public.incidents           for select to authenticated using ((select public.is_admin()));

-- node_api_credentials, sentinel_settings and rate_limits: RLS on, NO policies
-- => unreachable for anon/authenticated. Only the service role (bypasses RLS) touches them.

grant select on public.admin_users, public.nodes, public.node_heartbeats, public.services,
                public.service_checks, public.metrics, public.node_latest_metrics, public.incidents
  to authenticated;

-- Columns of nodes that must never be readable by dashboards are not present
-- (credentials live in their own table). anon gets nothing at all.
