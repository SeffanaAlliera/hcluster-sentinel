-- Run ONCE in the Supabase SQL editor, after you created your user under Authentication -> Users.
-- Replace the e-mail. Only users listed here can read any dashboard data (enforced by RLS).
insert into public.admin_users (user_id)
select id from auth.users where email = 'you@example.com'
on conflict do nothing;
select user_id from public.admin_users;   -- should return 1 row
