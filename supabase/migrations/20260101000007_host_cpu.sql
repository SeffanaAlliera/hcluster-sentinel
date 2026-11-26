-- CPU model name + logical CPU count shown on the node page. Additive and idempotent; safe on live data.
-- After running this, RE-RUN 20260101000003_functions.sql (create-or-replace) so ingest stores the new fields.
alter table public.nodes add column if not exists cpu_model text;
alter table public.nodes add column if not exists cpu_cores integer;
