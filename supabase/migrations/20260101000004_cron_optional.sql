-- =====================================================================
-- OPTIONAL but recommended: schedule offline detection + retention inside Postgres.
-- Requires the pg_cron extension (Supabase: Database -> Extensions -> pg_cron, or run below).
-- If you skip this, offline detection still happens lazily when the dashboard loads and
-- via /api/cron/evaluate (see docs), but incidents are only recorded when something runs.
-- =====================================================================
create extension if not exists pg_cron;

select cron.schedule('sentinel-evaluate', '* * * * *',  $$select public.evaluate_nodes()$$);
select cron.schedule('sentinel-purge',    '17 3 * * *', $$select public.purge_old_data()$$);
