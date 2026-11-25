// Runs the real migrations in an in-process Postgres (PGlite) and exercises ingest,
// incidents, offline detection, retention, rate limiting and RLS.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'migrations');
const db = new PGlite();
let passed = 0, failed = 0;
const ok = (c, m) => { if (c) { passed++; console.log('  ✔', m); } else { failed++; console.log('  ✘ FAIL:', m); } };
const q = async (sql, params) => (await db.query(sql, params)).rows;
const one = async (sql, params) => (await q(sql, params))[0];
const throws = async (fn, re, m) => { try { await fn(); ok(false, m + ' (no error)'); } catch (e) { ok(re.test(String(e.message)), `${m} [${e.message.slice(0, 70)}]`); } };

// --- minimal Supabase stand-ins -------------------------------------------
await db.exec(`
  create role anon nologin; create role authenticated nologin; create role service_role nologin;
  create schema auth;
  create table auth.users (id uuid primary key default gen_random_uuid(), email text);
  create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  grant usage on schema public, auth to anon, authenticated, service_role;
  grant execute on function auth.uid() to anon, authenticated, service_role;
`);

for (const f of readdirSync(dir).sort().filter(f => !f.includes('cron'))) {
  await db.exec(readFileSync(path.join(dir, f), 'utf8'));
  console.log('applied', f);
}

const NOW = () => new Date().toISOString();
const ago = (s) => new Date(Date.now() - s * 1000).toISOString();
const svc = (key, status, extra = {}) => ({ key, name: key[0].toUpperCase() + key.slice(1), type: 'http', target: 'http://127.0.0.1:1/x', status, response_ms: 12, http_status: status === 'down' ? null : 200, error: status === 'down' ? 'connection refused' : null, ...extra });
const report = (o = {}) => ({ collected_at: NOW(), agent: { version: '1.0.0' }, host: { hostname: 'h1', os: 'Ubuntu', arch: 'x86_64', uptime_seconds: 1000 }, ...o });
const metricsReport = (status = 'ok', prom = 'ok', values = [{ name: 'cpu_percent', value: 10, source: 'prometheus' }]) =>
  ({ status, prometheus: { status: prom, error: prom === 'unreachable' ? 'connection refused' : null, targets_up: 3, targets_total: 4, down_targets: ['node/x:9100'] }, values, unavailable: [] });
const ingest = (nodeId, credId, reports) => one('select public.ingest_report($1,$2,$3::jsonb) as r', [nodeId, credId, JSON.stringify(reports)]).then(x => x.r);
const node = (id) => one('select * from public.nodes where id=$1', [id]);
const openInc = (id) => q("select kind, service_id from public.incidents where node_id=$1 and resolved_at is null order by kind", [id]);

console.log('\nregistration');
const reg = await one("select * from public.register_node('h1','Home 1','hash1')");
const N = reg.node_id, C = reg.credential_id;
ok(!!N && !!C, 'register_node returns node + credential ids');
ok((await node(N)).status === 'unknown', 'new node starts UNKNOWN');
await throws(() => q("select public.register_node('x','y','h',60,30)"), /nodes_threshold_gt_interval/, 'threshold must exceed interval');

console.log('\nhealthy ingest');
let r = await ingest(N, C, [report({ services: [svc('jellyfin', 'operational'), svc('samba', 'operational', { type: 'tcp' })], metrics_report: metricsReport() })]);
ok(r.accepted === 1, 'one report accepted');
let n = await node(N);
ok(n.status === 'online', 'node ONLINE after healthy report');
ok(n.prometheus_status === 'ok' && n.metrics_status === 'ok', 'prometheus + metrics status ok');
ok(n.prometheus_targets_up === 3 && n.prometheus_targets_total === 4, 'target counts stored');
ok(n.os === 'Ubuntu' && n.arch === 'x86_64', 'host info stored');
ok((await q('select * from public.services where node_id=$1', [N])).length === 2, 'services auto-registered');
ok((await one("select value from public.node_latest_metrics where node_id=$1 and name='cpu_percent'", [N])).value === 10, 'latest metric stored');
ok((await openInc(N)).length === 0, 'no incidents');

console.log('\nservice failure => DEGRADED, not OFFLINE');
await ingest(N, C, [report({ services: [svc('jellyfin', 'down')] })]);
n = await node(N);
ok(n.status === 'degraded', 'node DEGRADED when a service is down');
ok(/service/.test(n.status_reason), 'status_reason explains why');
let inc = await openInc(N);
ok(inc.length === 1 && inc[0].kind === 'service_down', 'service_down incident opened');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 1000).toISOString(), services: [svc('jellyfin', 'down')] })]);
ok((await openInc(N)).length === 1, 'repeat failure does not duplicate incident');
const s = await one("select * from public.services where node_id=$1 and key='jellyfin'", [N]);
ok(s.status === 'down' && s.last_error === 'connection refused' && s.last_success_at, 'service state: down, last_error + last_success_at kept');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 2000).toISOString(), services: [svc('jellyfin', 'operational')] })]);
n = await node(N);
ok(n.status === 'online', 'node back to ONLINE after recovery');
const resolved = await one("select * from public.incidents where node_id=$1 and kind='service_down'", [N]);
ok(resolved.resolved_at && resolved.resolved_reason === 'service recovered', 'service incident resolved');

console.log('\nPrometheus outage is separate from node availability');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 3000).toISOString(), metrics_report: metricsReport('failed', 'unreachable', []) })]);
n = await node(N);
ok(n.status === 'degraded' && n.prometheus_status === 'unreachable', 'prometheus unreachable => DEGRADED (not offline)');
inc = await openInc(N);
ok(inc.map(i => i.kind).join() === 'prometheus_unavailable', 'only prometheus_unavailable incident (no duplicate metrics_failure)');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 4000).toISOString(), metrics_report: metricsReport('failed', 'ok', []) })]);
inc = await openInc(N);
ok(inc.map(i => i.kind).join() === 'metrics_failure', 'prometheus back but collection failing => metrics_failure incident');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 5000).toISOString(), metrics_report: metricsReport('partial', 'ok') })]);
ok((await openInc(N)).length === 0 && (await node(N)).status === 'online', 'partial metrics is not an incident; node ONLINE');

console.log('\nmuted services');
await q("update public.services set enabled=false where node_id=$1 and key='samba'", [N]);
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 6000).toISOString(), services: [svc('samba', 'down', { type: 'tcp' })] })]);
ok((await openInc(N)).length === 0 && (await node(N)).status === 'online', 'muted service: no incident, no degradation');
await q("update public.services set enabled=true where node_id=$1 and key='samba'", [N]);
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 7000).toISOString(), services: [svc('samba', 'operational', { type: 'tcp' })] })]);

console.log('\noffline detection');
await q("update public.nodes set last_heartbeat_at = now() - interval '10 minutes' where id=$1", [N]);
ok((await one('select public.evaluate_nodes() as c')).c === 1, 'evaluate_nodes flags 1 node');
n = await node(N);
ok(n.status === 'offline', 'stale heartbeat => OFFLINE');
inc = await openInc(N);
ok(inc.length === 1 && inc[0].kind === 'node_offline', 'node_offline incident opened');
ok((await one("select count(*)::int c from public.services where node_id=$1 and status='unknown'", [N])).c === 2, 'services UNKNOWN while node silent');
ok((await one('select public.evaluate_nodes() as c')).c === 0, 'evaluate is idempotent');
const startedAt = (await one("select started_at from public.incidents where kind='node_offline' and node_id=$1", [N])).started_at;
ok(Date.now() - new Date(startedAt).getTime() > 9 * 60 * 1000, 'outage start = last heartbeat, not detection time');
await ingest(N, C, [report({ collected_at: new Date(Date.now() + 8000).toISOString(), services: [svc('jellyfin', 'operational'), svc('samba', 'operational', { type: 'tcp' })] })]);
n = await node(N);
ok(n.status === 'online', 'heartbeat resumes => ONLINE');
ok(!!(await one("select resolved_at from public.incidents where kind='node_offline' and node_id=$1", [N])).resolved_at, 'node_offline incident resolved (recovery event)');

console.log('\nbacklog replay / ordering / idempotency');
const t0 = Date.now() - 600_000;
const mk = (i, cpu) => report({ collected_at: new Date(t0 + i * 60_000).toISOString(), metrics_report: metricsReport('ok', 'ok', [{ name: 'cpu_percent', value: cpu, source: 'prometheus' }]) });
const before = (await one('select count(*)::int c from public.metrics where node_id=$1', [N])).c;
r = await ingest(N, C, [mk(3, 33), mk(1, 11), mk(2, 22)]);
ok(r.accepted === 3, 'unordered backlog batch accepted');
ok((await one('select count(*)::int c from public.metrics where node_id=$1', [N])).c === before + 3, 'backlog metrics stored with original timestamps');
await ingest(N, C, [mk(1, 11)]);
ok((await one('select count(*)::int c from public.metrics where node_id=$1', [N])).c === before + 3, 'replayed report is idempotent');
ok((await one("select value from public.node_latest_metrics where node_id=$1 and name='cpu_percent'", [N])).value === 10, 'older backlog does not overwrite newer latest value');
r = await ingest(N, C, [report({ collected_at: ago(3 * 86400) })]);
ok(r.skipped === 1 && r.accepted === 0, 'reports older than 24h are dropped');
const fut = await ingest(N, C, [report({ collected_at: new Date(Date.now() + 3600_000).toISOString() })]);
ok(fut.accepted === 1, 'future timestamp is clamped, not rejected');
ok((await one('select max(ts) m from public.node_heartbeats where node_id=$1', [N])).m <= new Date(Date.now() + 60_000), 'a +1h timestamp was clamped (no heartbeat beyond the 5-min skew allowance)');

console.log('\nmetric_series');
const ser = await q("select * from public.metric_series($1, array['cpu_percent'], now() - interval '1 hour', now() + interval '1 hour', 300)", [N]);
ok(ser.length > 0 && ser.every(x => x.name === 'cpu_percent'), 'metric_series returns bucketed rows');

console.log('\ndisabled node / unknown node');
await q('update public.nodes set enabled=false where id=$1', [N]);
await throws(() => ingest(N, C, [report()]), /node_disabled/, 'disabled node rejected');
await q('update public.nodes set enabled=true where id=$1', [N]);
await throws(() => ingest('00000000-0000-0000-0000-0000000000aa', C, [report()]), /node_not_found/, 'unknown node rejected');

console.log('\ncredential rotation');
const C2 = (await one("select public.rotate_node_credential($1,'hash2') as c", [N])).c;
ok((await one('select revoked_at from public.node_api_credentials where id=$1', [C])).revoked_at, 'old credential revoked');
ok(!(await one('select revoked_at from public.node_api_credentials where id=$1', [C2])).revoked_at, 'new credential active');

console.log('\nrate limiting');
const hits = []; for (let i = 0; i < 5; i++) hits.push((await one("select public.rate_limit_hit('k',3,60) as a")).a);
ok(hits.join() === 'true,true,true,false,false', 'rate_limit_hit allows 3 then blocks');
ok((await one("select public.rate_limit_blocked('k',3,60) as b")).b === true, 'rate_limit_blocked reflects window');
await q("update public.rate_limits set window_start = now() - interval '2 minutes' where key='k'");
ok((await one("select public.rate_limit_hit('k',3,60) as a")).a === true, 'window resets');

console.log('\nretention');
await q("insert into public.metrics(node_id,name,ts,value) values ($1,'cpu_percent', now() - interval '30 days', 1)", [N]);
await q("insert into public.node_heartbeats(node_id,ts) values ($1, now() - interval '30 days')", [N]);
const p = (await one('select public.purge_old_data() as p')).p;
ok(p.metrics >= 1 && p.heartbeats >= 1, `purge removed old rows ${JSON.stringify(p)}`);
ok((await one("select count(*)::int c from public.metrics where ts < now() - interval '14 days'")).c === 0, 'nothing older than retention remains');

console.log('\nRLS / privileges');
const uid = '11111111-1111-1111-1111-111111111111', uid2 = '22222222-2222-2222-2222-222222222222';
await q("insert into auth.users(id,email) values ($1,'admin@x'),($2,'rando@x')", [uid, uid2]);
await q('insert into public.admin_users(user_id) values ($1)', [uid]);
const as = async (role, sub, fn) => { await db.exec(`set role ${role}; select set_config('request.jwt.claim.sub','${sub}',false)`); try { return await fn(); } finally { await db.exec('reset role'); } };
ok((await as('authenticated', uid, () => q('select * from public.nodes'))).length === 1, 'admin can read nodes');
ok((await as('authenticated', uid, () => q('select * from public.services'))).length > 0, 'admin can read services');
ok((await as('authenticated', uid2, () => q('select * from public.nodes'))).length === 0, 'non-admin authenticated user reads 0 nodes');
ok((await as('authenticated', uid2, () => q('select * from public.metrics'))).length === 0, 'non-admin reads 0 metrics');
ok((await as('authenticated', uid2, () => q("select * from public.metric_series($1, array['cpu_percent'], now() - interval '1 day', now(), 60)", [N]))).length === 0, 'non-admin metric_series returns nothing (invoker RLS)');
await throws(() => as('authenticated', uid, () => q('select * from public.node_api_credentials')), /permission denied/, 'admin cannot read node_api_credentials from client role');
await throws(() => as('authenticated', uid, () => q('select * from public.rate_limits')), /permission denied/, 'client role cannot read rate_limits');
await throws(() => as('authenticated', uid, () => q("update public.nodes set display_name='x'")), /permission denied/, 'client role cannot write nodes');
await throws(() => as('authenticated', uid, () => q('select public.evaluate_nodes()')), /permission denied/, 'client role cannot call evaluate_nodes');
await throws(() => as('authenticated', uid, () => ingest(N, C2, [report()])), /permission denied/, 'client role cannot call ingest_report');
await throws(() => as('anon', uid, () => q('select * from public.nodes')), /permission denied/, 'anon has no table access');
await throws(() => as('anon', uid, () => q('select public.is_admin()')), /permission denied/, 'anon cannot call is_admin');
ok((await as('service_role', uid, () => q('select public.evaluate_nodes()'))).length === 1, 'service_role can call evaluate_nodes');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
