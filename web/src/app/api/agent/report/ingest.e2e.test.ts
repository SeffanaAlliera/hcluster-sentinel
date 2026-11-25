import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import http from 'node:http';
import { execFile } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { NextRequest } from 'next/server';
import { buildToken, hashSecret, newSecret } from '@/lib/tokens';
import { makeDb } from '@/test/pgAdmin';

let ctx: Awaited<ReturnType<typeof makeDb>>;
vi.mock('@/lib/supabase/admin', () => ({ adminClient: () => ctx.client }));

let POST: (r: NextRequest) => Promise<Response>;
let server: http.Server; let base: string;
let nodeId: string, token: string, credId: string;

const NODE_LIMIT = 6;
beforeAll(async () => {
  process.env.NODE_RATE_LIMIT_PER_MIN = String(NODE_LIMIT);
  ctx = await makeDb();
  ({ POST } = await import('./route'));
  const secret = newSecret();
  const r = await ctx.db.query(`select * from public.register_node('h1','Home 1',$1)`, [hashSecret(secret)]);
  nodeId = (r.rows[0] as { node_id: string }).node_id; credId = (r.rows[0] as { credential_id: string }).credential_id;
  token = buildToken(credId, secret);
  server = http.createServer(async (req, res) => {
    const chunks: Buffer[] = []; for await (const c of req) chunks.push(c as Buffer);
    const headers = new Headers(); for (const [k, v] of Object.entries(req.headers)) if (typeof v === 'string') headers.set(k, v);
    const out = await POST(new NextRequest(`http://localhost${req.url}`, { method: 'POST', headers, body: Buffer.concat(chunks) }));
    res.writeHead(out.status, Object.fromEntries(out.headers)); res.end(await out.text());
  }).listen(0, '127.0.0.1');
  await new Promise((r) => server.on('listening', r));
  base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
});
afterAll(() => server?.close());

const body = (o: object = {}, nid = () => nodeId) => JSON.stringify({ schema: 1, node_id: nid(), reports: [{ collected_at: new Date().toISOString(), host: { hostname: 'h1' }, ...o }] });
const send = (b: string, tok: string | null = token, ip = '9.9.9.9') => fetch(`${base}/`, { method: 'POST', body: b, headers: { 'Content-Type': 'application/json', 'x-real-ip': ip, ...(tok ? { Authorization: `Bearer ${tok}` } : {}) } });
const node = async () => (await ctx.db.query('select * from public.nodes where id=$1', [nodeId])).rows[0] as Record<string, unknown>;

describe('POST /api/agent/report (real route + real SQL)', () => {
  it('rejects missing / malformed / wrong-secret / unknown-credential tokens with 401', async () => {
    expect((await send(body(), null)).status).toBe(401);
    expect((await send(body(), 'garbage')).status).toBe(401);
    expect((await send(body(), buildToken(credId, newSecret()), '1.1.1.1')).status).toBe(401);
    expect((await send(body(), buildToken('00000000-0000-4000-8000-000000000000', newSecret()), '1.1.1.2')).status).toBe(401);
    expect((await node()).status).toBe('unknown');
  });
  it('accepts a valid report and updates node state', async () => {
    const r = await send(body({ services: [{ key: 'jellyfin', name: 'Jellyfin', type: 'http', status: 'down', error: 'refused' }] }));
    expect(r.status).toBe(200);
    expect((await r.json()).accepted).toBe(1);
    expect((await node()).status).toBe('degraded');
    expect((await ctx.db.query('select * from public.incidents where resolved_at is null')).rows).toHaveLength(1);
  });
  it('rejects a payload that names a different node (token decides identity)', async () => {
    const r = await send(body({}, () => '99999999-9999-4999-8999-999999999999'));
    expect(r.status).toBe(403);
  });
  it('rejects invalid payloads and bad JSON with 400', async () => {
    expect((await send('{nope')).status).toBe(400);
    expect((await send(body({ services: [{ key: 'BAD KEY', name: 'x', type: 'http', status: 'up' }] }))).status).toBe(400);
  });
  it('per-node rate limit returns 429 with Retry-After', async () => {
    const codes: number[] = [];
    for (let i = 0; i < NODE_LIMIT + 2; i++) codes.push((await send(body())).status);
    expect(codes.slice(-1)[0]).toBe(429);
    expect((await send(body())).headers.get('retry-after')).toBeTruthy();
  });
  it('blocks an IP after repeated failed authentication', async () => {
    let last = 0;
    for (let i = 0; i < 22; i++) last = (await send(body(), buildToken(credId, newSecret()), '6.6.6.6')).status;
    expect(last).toBe(429);
    // and that IP is now blocked even with a valid token
    expect((await send(body(), token, '6.6.6.6')).status).toBe(429);
  });
  it('revoked credentials stop working; disabled nodes get 403', async () => {
    await ctx.db.query('delete from public.rate_limits');
    await ctx.db.query('update public.nodes set enabled=false where id=$1', [nodeId]);
    expect((await send(body())).status).toBe(403);
    await ctx.db.query('update public.nodes set enabled=true where id=$1', [nodeId]);
    await ctx.db.query('update public.node_api_credentials set revoked_at=now() where id=$1', [credId]);
    expect((await send(body())).status).toBe(401);
    await ctx.db.query('update public.node_api_credentials set revoked_at=null where id=$1', [credId]);
  });
  it('rejects oversized bodies with 413', async () => {
    await ctx.db.query('delete from public.rate_limits');
    expect((await send('x'.repeat(300 * 1024))).status).toBe(413);
  });
});

describe('real Python agent -> route -> Postgres, incl. failure simulation', () => {
  it('heartbeat, service checks and local metrics arrive; outage + recovery are recorded', async () => {
    await ctx.db.query('delete from public.rate_limits');
    await ctx.db.query('delete from public.incidents');
    const tmp = mkdtempSync(path.join(os.tmpdir(), 'agent-'));
    // a real listening TCP port (the "service") plus a closed one
    const up = http.createServer((_, r) => r.end('ok')).listen(0, '127.0.0.1'); await new Promise((r) => up.on('listening', r));
    const upPort = (up.address() as { port: number }).port;
    const cfg = path.join(tmp, 'config.yaml');
    writeFileSync(cfg, `node: {id: "${nodeId}"}\napi: {url: "${base}"}\nstate_dir: "${tmp}"\nprometheus: {enabled: false}\nservices:\n` +
      `  - {key: web, name: Web, type: http, url: "http://127.0.0.1:${upPort}/", retries: 0}\n  - {key: closed, name: Closed, type: tcp, host: 127.0.0.1, port: 1, retries: 0, timeout_seconds: 1}\n`);
    const agentDir = path.resolve(__dirname, '../../../../../../agent');
    // async spawn: the in-process HTTP server must keep serving while the agent runs
    const run = () => new Promise<{ status: number; stderr: string }>((resolve) => {
      execFile('python3', ['-m', 'hcluster_agent', '-c', cfg, 'once', '--send'], { cwd: agentDir, env: { ...process.env, PYTHONPATH: agentDir, HCLUSTER_TOKEN: token } },
        (err, _o, stderr) => resolve({ status: err ? ((err as { code?: number }).code ?? 1) : 0, stderr }));
    });

    const res = await run();
    expect(res.status, res.stderr).toBe(0);
    up.close();
    const n = await node();
    expect(n.status).toBe('degraded');                                   // "closed" service is down, node still alive
    expect(n.prometheus_status).toBe('disabled');
    const svc = (await ctx.db.query('select key,status from public.services order by key')).rows;
    expect(svc).toEqual(expect.arrayContaining([{ key: 'web', status: 'operational' }, { key: 'closed', status: 'down' }]));
    expect(Number(((await ctx.db.query(`select count(*) c from public.metrics where source='local'`)).rows[0] as { c: string }).c)).toBeGreaterThan(2);

    // multi-drive detection + per-NIC + labels survive the full path
    const disks = (await ctx.db.query(`select label, value from public.node_latest_metrics where name='disk_percent' order by label`)).rows as { label: string; value: number }[];
    expect(disks.length).toBeGreaterThan(0);
    expect(disks.every((d) => d.label.startsWith('/'))).toBe(true);        // mountpoint labels
    expect(disks.map((d) => d.label)).toContain('/');

    // simulate node failure: heartbeats stop
    await ctx.db.query(`update public.nodes set last_heartbeat_at = now() - interval '10 minutes' where id=$1`, [nodeId]);
    await ctx.client.rpc('evaluate_nodes');
    expect((await node()).status).toBe('offline');
    expect((await ctx.db.query(`select 1 from public.incidents where kind='node_offline' and resolved_at is null`)).rows).toHaveLength(1);

    // recovery: agent comes back
    expect((await run()).status).toBe(0);
    expect((await node()).status).toBe('degraded');
    expect((await ctx.db.query(`select 1 from public.incidents where kind='node_offline' and resolved_at is not null`)).rows).toHaveLength(1);
  });
});
