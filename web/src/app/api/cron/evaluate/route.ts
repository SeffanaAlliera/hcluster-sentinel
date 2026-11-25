import { NextResponse, type NextRequest } from 'next/server';
import { timingSafeEqual } from 'node:crypto';
import { adminClient } from '@/lib/supabase/admin';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

function authorised(req: NextRequest): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret || secret.length < 32) return false;               // refuse to run unprotected
  const given = /^Bearer\s+(.+)$/i.exec(req.headers.get('authorization') ?? '')?.[1] ?? '';
  const a = Buffer.from(given), b = Buffer.from(secret);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Vercel Cron calls GET with `Authorization: Bearer $CRON_SECRET`. Safe to call as often as you like.
export async function GET(req: NextRequest) {
  if (!authorised(req)) return NextResponse.json({ error: 'unauthorized' }, { status: 401 });
  const db = adminClient();
  const evaluated = await db.rpc('evaluate_nodes');
  const purge = new Date().getUTCHours() === 3 || req.nextUrl.searchParams.get('purge') === '1' ? await db.rpc('purge_old_data') : null;
  if (evaluated.error) return NextResponse.json({ error: 'evaluate_failed' }, { status: 500 });
  return NextResponse.json({ ok: true, nodes_marked_offline: evaluated.data, purged: purge?.data ?? null });
}
