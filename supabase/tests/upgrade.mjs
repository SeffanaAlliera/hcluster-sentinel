// Proves the upgrade path: build the PREVIOUS schema, fill it with data, upgrade, and check nothing is lost.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'migrations');
const f = (n) => readFileSync(path.join(dir, n), 'utf8');
let failed = 0;
const ok = (c, m) => { console.log(c ? '  ✔' : '  ✘ FAIL:', m); if (!c) failed++; };

// ---- reconstruct the previous release's SQL by reverting this release's changes ----
const rep = (s, a, b) => { if (!s.includes(a)) throw new Error('revert pattern missing: ' + a.slice(0, 50)); return s.replace(a, b); };
let oldSchema = f('20260101000001_schema.sql');
oldSchema = rep(oldSchema, "as enum ('http', 'tcp', 'dns');", "as enum ('http', 'tcp');");
oldSchema = rep(oldSchema, "  label   text not null default '' check (char_length(label) <= 64),   -- '' = node-wide; else mountpoint / interface name\n", '');
oldSchema = rep(oldSchema, 'primary key (node_id, name, label, ts)', 'primary key (node_id, name, ts)');
oldSchema = rep(oldSchema, "  label   text not null default '',\n", '');
oldSchema = rep(oldSchema, 'primary key (node_id, name, label)', 'primary key (node_id, name)');
let oldFns = f('20260101000003_functions.sql');
oldFns = rep(oldFns, "insert into public.metrics (node_id, name, label, ts, value, source)\n    select p_node_id, e->>'name', coalesce(left(e->>'label', 64), ''), v_ts,", "insert into public.metrics (node_id, name, ts, value, source)\n    select p_node_id, e->>'name', v_ts,");
oldFns = rep(oldFns, "insert into public.node_latest_metrics as l (node_id, name, label, value, source, ts)\n    select p_node_id, e->>'name', coalesce(left(e->>'label', 64), ''), (e->>'value')", "insert into public.node_latest_metrics as l (node_id, name, value, source, ts)\n    select p_node_id, e->>'name', (e->>'value')");
oldFns = rep(oldFns, 'on conflict (node_id, name, label) do update', 'on conflict (node_id, name) do update');
oldFns = rep(oldFns, 'returns table (bucket timestamptz, name text, label text, value double precision)', 'returns table (bucket timestamptz, name text, value double precision)');
oldFns = rep(oldFns, '         m.name, m.label, avg(m.value)::double precision', '         m.name, avg(m.value)::double precision');
oldFns = rep(oldFns, '   group by 1, 2, 3\n   order by 1, 2, 3\n', '   group by 1, 2\n   order by 1\n');

oldFns = rep(oldFns, "      cpu_model      = coalesce(left(v_host->>'cpu_model', 120), cpu_model),\n      cpu_cores      = coalesce(nullif(v_host->>'cpu_cores', '')::integer, cpu_cores),\n", '');

oldFns = rep(oldFns, "      timezone       = coalesce((select z.name from pg_timezone_names z where z.name = left(v_host->>'timezone', 64) limit 1), timezone),\n", '');
oldFns = rep(oldFns, "'No heartbeat since ' || to_char(n.last_heartbeat_at at time zone public.safe_tz(n.timezone), 'YYYY-MM-DD HH24:MI:SS') || ' ' || public.safe_tz(n.timezone),", "'No heartbeat since ' || to_char(n.last_heartbeat_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS \"UTC\"'),");

const db = new PGlite();
await db.exec(`create role anon nologin; create role authenticated nologin; create role service_role nologin;
  create schema auth; create table auth.users (id uuid primary key default gen_random_uuid(), email text);
  create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
  grant usage on schema public, auth to anon, authenticated, service_role;`);
for (const s of [oldSchema, f('20260101000002_rls.sql'), oldFns]) await db.exec(s);
const q = async (s, p) => (await db.query(s, p)).rows;

console.log('previous release: data in place');
const reg = (await q("select * from public.register_node('h1','Home 1','hash-of-existing-token')"))[0];
const rep1 = [{ collected_at: new Date().toISOString(), host: { os: 'Ubuntu' },
  services: [{ key: 'jellyfin', name: 'Jellyfin', type: 'http', status: 'operational' }],
  metrics_report: { status: 'ok', prometheus: { status: 'ok' }, values: [{ name: 'cpu_percent', value: 12, source: 'prometheus' }, { name: 'disk_percent', value: 40, source: 'prometheus' }], unavailable: [] } }];
await db.query('select public.ingest_report($1,$2,$3::jsonb)', [reg.node_id, reg.credential_id, JSON.stringify(rep1)]);
ok((await q('select count(*)::int c from public.metrics'))[0].c === 2, 'old schema stored 2 metric rows');

console.log('apply upgrade (+ re-run functions, process enum) — twice, to prove idempotence');
for (let i = 0; i < 2; i++) for (const n of ['20260101000006_upgrade_labels.sql', '20260101000007_host_cpu.sql', '20260101000008_node_timezone.sql', '20260101000003_functions.sql', '20260101000005_process_check.sql']) await db.exec(f(n));

console.log('data preserved');
const node = (await q('select * from public.nodes'))[0];
ok(node.id === reg.node_id && node.hostname === 'h1' && node.status === 'online', 'node, status intact');
const cred = (await q('select * from public.node_api_credentials'))[0];
ok(cred.id === reg.credential_id && cred.secret_hash === 'hash-of-existing-token' && !cred.revoked_at, 'API token hash + credential id unchanged (existing agents keep working)');
ok((await q("select count(*)::int c from public.metrics where label = ''"))[0].c === 2, 'old metric rows kept, label = empty (node-wide)');
ok((await q("select count(*)::int c from public.node_latest_metrics where label = ''"))[0].c === 2, 'latest metrics kept');
ok((await q("select count(*)::int c from public.services where key='jellyfin'"))[0].c === 1, 'services kept');
ok((await q('select cpu_model from public.nodes'))[0].cpu_model === null, 'new cpu_model column exists, empty until the agent reports it');

console.log('new features work on upgraded DB');
const rep2 = [{ collected_at: new Date(Date.now() + 5000).toISOString(), host: { cpu_model: 'Intel(R) Celeron(R) CPU N4100', cpu_cores: 4, timezone: 'Asia/Manila' },
  services: [{ key: 'dnsmasq-dhcp', name: 'dnsmasq', type: 'process', status: 'operational' }],
  metrics_report: { status: 'ok', prometheus: { status: 'disabled' }, values: [{ name: 'disk_percent', label: '/mnt/media', value: 70, source: 'local' }, { name: 'disk_percent', label: '/', value: 41, source: 'local' }], unavailable: [] } }];
await db.query('select public.ingest_report($1,$2,$3::jsonb)', [reg.node_id, reg.credential_id, JSON.stringify(rep2)]);
const disks = await q("select label, value from public.node_latest_metrics where name='disk_percent' order by label");
ok(disks.map((d) => d.label).join('|') === '|/|/mnt/media', 'per-drive labels coexist with the old node-wide row');
ok((await q("select check_type from public.services where key='dnsmasq-dhcp'"))[0].check_type === 'process', "'process' service type accepted");
const nrow = (await q('select cpu_model, cpu_cores from public.nodes'))[0];
ok(nrow.cpu_model === 'Intel(R) Celeron(R) CPU N4100' && nrow.cpu_cores === 4, 'ingest stores CPU model + cores');
ok((await q('select timezone from public.nodes'))[0].timezone === 'Asia/Manila', 'ingest stores the node timezone');
const ser = await q("select * from public.metric_series($1, array['disk_percent'], now() - interval '1 hour', now() + interval '1 hour', 300)", [reg.node_id]);
ok(ser.length >= 3 && 'label' in ser[0], 'metric_series returns labels');
await db.exec('set role service_role'); await q('select public.evaluate_nodes()'); await db.exec('reset role');
ok(true, 'service_role can still execute functions');

console.log('no-op on a fresh install');
const fresh = new PGlite();
await fresh.exec(`create role anon nologin; create role authenticated nologin; create role service_role nologin;
  create schema auth; create table auth.users (id uuid primary key default gen_random_uuid(), email text);
  create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$; grant usage on schema public, auth to anon, authenticated, service_role;`);
for (const n of ['20260101000001_schema.sql', '20260101000002_rls.sql', '20260101000003_functions.sql', '20260101000005_process_check.sql', '20260101000006_upgrade_labels.sql', '20260101000007_host_cpu.sql', '20260101000008_node_timezone.sql']) await fresh.exec(f(n));
ok((await fresh.query("select count(*)::int c from pg_proc where proname='metric_series'")).rows[0].c === 1, 'fresh install: 0006 leaves metric_series alone');
console.log(failed ? `\n${failed} FAILED` : '\nupgrade path OK');
process.exit(failed ? 1 : 0);
