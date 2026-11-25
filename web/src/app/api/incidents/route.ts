import { NextResponse, type NextRequest } from 'next/server';
import { requireAdminApi } from '@/lib/auth';
import { listIncidents } from '@/lib/data';
export const dynamic = 'force-dynamic';
export async function GET(req: NextRequest) {
  const a = await requireAdminApi(req);
  if ('error' in a) return a.error;
  const sp = req.nextUrl.searchParams;
  return NextResponse.json({ incidents: await listIncidents(a.admin.supabase, { openOnly: sp.get('open') === '1', limit: Math.min(Number(sp.get('limit')) || 100, 500) }) });
}
