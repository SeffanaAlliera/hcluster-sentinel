import { NextResponse, type NextRequest } from 'next/server';
import { requireAdminApi } from '@/lib/auth';
import { overview } from '@/lib/data';
export const dynamic = 'force-dynamic';
export async function GET(req: NextRequest) {
  const a = await requireAdminApi(req);
  if ('error' in a) return a.error;
  return NextResponse.json(await overview(a.admin.supabase));
}
