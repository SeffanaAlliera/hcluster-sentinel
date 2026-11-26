-- The node's own timezone (IANA name, reported by the agent) so the dashboard can show every time in node-local time.
-- Additive and idempotent. After running this, RE-RUN 20260101000003_functions.sql (create-or-replace).
alter table public.nodes add column if not exists timezone text;
