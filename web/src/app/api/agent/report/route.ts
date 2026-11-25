import { NextResponse, type NextRequest } from 'next/server';
import { adminClient } from '@/lib/supabase/admin';
import { agentPayload } from '@/lib/validation';
import { parseBearer, parseToken, verifySecret } from '@/lib/tokens';
import { clientIp, ipKey, memoryLimit } from '@/lib/ratelimit';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const MAX_BODY = 256 * 1024;
const NODE_LIMIT = Number(process.env.NODE_RATE_LIMIT_PER_MIN ?? 30);   // requests / node / minute
const AUTHFAIL_LIMIT = 20;                                              // failed auths / IP / 10 min
const json = (body: object, status: number, headers?: Record<string, string>) => NextResponse.json(body, { status, headers });
const unauthorized = () => json({ error: 'unauthorized' }, 401, { 'WWW-Authenticate': 'Bearer' });

export async function POST(req: NextRequest) {
  const ip = ipKey(clientIp(req.headers));

  // 1) cheap per-instance flood guard (before any I/O)
  if (!memoryLimit(`ip:${ip}`, 120, 60_000)) return json({ error: 'rate_limited' }, 429, { 'Retry-After': '30' });
  const declared = Number(req.headers.get('content-length') ?? 0);
  if (declared > MAX_BODY) return json({ error: 'payload_too_large' }, 413);

  const db = adminClient();

  // 2) shared (DB) limiter for repeated failed authentication from one IP
  const blocked = await db.rpc('rate_limit_blocked', { p_key: `authfail:${ip}`, p_limit: AUTHFAIL_LIMIT, p_window_seconds: 600 });
  if (blocked.data === true) return json({ error: 'rate_limited' }, 429, { 'Retry-After': '600' });

  // 3) authenticate the node BEFORE reading the body
  const tok = parseToken(parseBearer(req.headers.get('authorization')));
  type Cred = { id: string; node_id: string; secret_hash: string; revoked_at: string | null };
  let cred = null as Cred | null;
  if (tok) {
    const { data } = await db.from('node_api_credentials').select('id,node_id,secret_hash,revoked_at').eq('id', tok.credentialId).maybeSingle();
    cred = data as Cred | null;
  }
  // always do the hash comparison so valid/invalid ids take similar time
  const c: Cred | null = cred;
  const valid = !!tok && verifySecret(tok.secret, c?.secret_hash ?? '0'.repeat(64)) && !!c && !c.revoked_at;
  if (!valid || !c) {
    await db.rpc('rate_limit_hit', { p_key: `authfail:${ip}`, p_limit: AUTHFAIL_LIMIT, p_window_seconds: 600 });
    return unauthorized();
  }

  // 4) per-node limiter (shared across serverless instances)
  const rl = await db.rpc('rate_limit_hit', { p_key: `ingest:${c.node_id}`, p_limit: NODE_LIMIT, p_window_seconds: 60 });
  if (rl.data === false) return json({ error: 'rate_limited' }, 429, { 'Retry-After': '30' });

  // 5) validate + sanitise
  const text = await req.text();
  if (text.length > MAX_BODY) return json({ error: 'payload_too_large' }, 413);
  let raw: unknown;
  try { raw = JSON.parse(text); } catch { return json({ error: 'invalid_json' }, 400); }
  const parsed = agentPayload.safeParse(raw);
  if (!parsed.success) {
    return json({ error: 'invalid_payload', issues: parsed.error.issues.slice(0, 5).map((i) => `${i.path.join('.')}: ${i.message}`) }, 400);
  }
  // the token decides which node this is; a payload naming another node is rejected
  if (parsed.data.node_id !== c.node_id) return json({ error: 'node_mismatch' }, 403);

  // 6) store (single transactional SQL function: heartbeat, services, metrics, incidents)
  const { data, error } = await db.rpc('ingest_report', { p_node_id: c.node_id, p_credential_id: c.id, p_reports: parsed.data.reports });
  if (error) {
    if (/node_disabled/.test(error.message)) return json({ error: 'node_disabled' }, 403);
    if (/node_not_found/.test(error.message)) return unauthorized();
    console.error('ingest_report failed', error.message);
    return json({ error: 'server_error' }, 500);
  }
  return json({ ok: true, ...(data as object) }, 200);
}
