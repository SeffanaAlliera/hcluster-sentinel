import { NextResponse, type NextRequest } from 'next/server';
import { z } from 'zod';
import { requireAdminApi } from '@/lib/auth';
import { metricSeries } from '@/lib/data';
import { parseRange } from '@/lib/validation';
export const dynamic = 'force-dynamic';

// History comes from Supabase, never from Prometheus.  GET ?range=1h|6h|24h|7d
export async function GET(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const a = await requireAdminApi(req);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!z.string().uuid().safeParse(id).success) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  try {
    return NextResponse.json(await metricSeries(a.admin.supabase, id, parseRange(req.nextUrl.searchParams.get('range'))));
  } catch { return NextResponse.json({ error: 'query_failed' }, { status: 500 }); }
}
