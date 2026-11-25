import 'server-only';
import { redirect } from 'next/navigation';
import { NextResponse, type NextRequest } from 'next/server';
import { userClient } from '@/lib/supabase/server';

export async function getAdmin() {
  const supabase = await userClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return null;
  const { data } = await supabase.from('admin_users').select('user_id').eq('user_id', user.id).maybeSingle();
  return data ? { supabase, user } : null;
}

export async function requireAdminPage() {
  const a = await getAdmin();
  if (!a) redirect('/login?error=forbidden');
  return a;
}

/** CSRF defence for cookie-authenticated mutations: Origin must match Host, body must be JSON. */
export function sameOrigin(req: NextRequest): boolean {
  const origin = req.headers.get('origin');
  const host = req.headers.get('x-forwarded-host') ?? req.headers.get('host');
  if (!origin || !host) return false;
  try { return new URL(origin).host === host; } catch { return false; }
}

/** For dashboard API routes. Returns the admin context or a ready-made error response. */
export async function requireAdminApi(req: NextRequest, mutating = false) {
  if (mutating && !sameOrigin(req)) return { error: NextResponse.json({ error: 'bad_origin' }, { status: 403 }) } as const;
  const a = await getAdmin();
  if (!a) return { error: NextResponse.json({ error: 'unauthorized' }, { status: 401 }) } as const;
  return { admin: a } as const;
}
