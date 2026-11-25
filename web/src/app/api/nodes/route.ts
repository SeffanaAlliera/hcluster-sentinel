import { NextResponse, type NextRequest } from 'next/server';
import { requireAdminApi } from '@/lib/auth';
import { listNodes } from '@/lib/data';
import { adminClient } from '@/lib/supabase/admin';
import { nodeCreate } from '@/lib/validation';
import { buildToken, hashSecret, newSecret } from '@/lib/tokens';
export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  const a = await requireAdminApi(req);
  if ('error' in a) return a.error;
  return NextResponse.json({ nodes: await listNodes(a.admin.supabase) });
}

/** Registers a node and returns its one-time token. Only a logged-in admin can do this. */
export async function POST(req: NextRequest) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const body = nodeCreate.safeParse(await req.json().catch(() => null));
  if (!body.success) return NextResponse.json({ error: 'invalid', issues: body.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`) }, { status: 400 });
  const secret = newSecret();
  const { data, error } = await adminClient().rpc('register_node', {
    p_hostname: body.data.hostname, p_display_name: body.data.display_name, p_secret_hash: hashSecret(secret),
    p_heartbeat_interval: body.data.heartbeat_interval_seconds, p_offline_threshold: body.data.offline_threshold_seconds,
  });
  const row = (data as { node_id: string; credential_id: string }[] | null)?.[0];
  if (error || !row) return NextResponse.json({ error: 'create_failed' }, { status: 500 });
  return NextResponse.json({ node_id: row.node_id, token: buildToken(row.credential_id, secret) }, { status: 201, headers: { 'Cache-Control': 'no-store' } });
}
