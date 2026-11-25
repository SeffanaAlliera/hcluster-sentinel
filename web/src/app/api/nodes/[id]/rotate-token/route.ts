import { NextResponse, type NextRequest } from 'next/server';
import { z } from 'zod';
import { requireAdminApi } from '@/lib/auth';
import { adminClient } from '@/lib/supabase/admin';
import { buildToken, hashSecret, newSecret } from '@/lib/tokens';
export const dynamic = 'force-dynamic';

/** Revokes the node's current credential(s) and issues a new one (shown once). */
export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!z.string().uuid().safeParse(id).success) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  const secret = newSecret();
  const { data, error } = await adminClient().rpc('rotate_node_credential', { p_node_id: id, p_secret_hash: hashSecret(secret) });
  if (error || !data) return NextResponse.json({ error: /node_not_found/.test(error?.message ?? '') ? 'not_found' : 'rotate_failed' }, { status: error ? 404 : 500 });
  return NextResponse.json({ token: buildToken(data as string, secret) }, { headers: { 'Cache-Control': 'no-store' } });
}
