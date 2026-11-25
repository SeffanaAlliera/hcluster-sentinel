import { NextResponse, type NextRequest } from 'next/server';
import { z } from 'zod';
import { requireAdminApi } from '@/lib/auth';
import { adminClient } from '@/lib/supabase/admin';
import { servicePatch } from '@/lib/validation';
export const dynamic = 'force-dynamic';
type Ctx = { params: Promise<{ id: string }> };

/** Mute / unmute a service (muted = no incidents, ignored in the node roll-up). */
export async function PATCH(req: NextRequest, { params }: Ctx) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const { id } = await params;
  const body = servicePatch.safeParse(await req.json().catch(() => null));
  if (!z.string().uuid().safeParse(id).success || !body.success) return NextResponse.json({ error: 'invalid' }, { status: 400 });
  const db = adminClient();
  const { data, error } = await db.from('services').update({ enabled: body.data.enabled }).eq('id', id).select('node_id').maybeSingle();
  if (error || !data) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  if (!body.data.enabled) await db.from('incidents').update({ resolved_at: new Date().toISOString(), resolved_reason: 'service muted' }).eq('service_id', id).is('resolved_at', null);
  await db.rpc('recompute_node_status', { p_node_id: data.node_id });
  return NextResponse.json({ ok: true });
}

/** Remove a service that is no longer in the agent's config (it reappears if the agent still reports it). */
export async function DELETE(req: NextRequest, { params }: Ctx) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!z.string().uuid().safeParse(id).success) return NextResponse.json({ error: 'invalid' }, { status: 400 });
  const db = adminClient();
  const { data } = await db.from('services').delete().eq('id', id).select('node_id').maybeSingle();
  if (data) await db.rpc('recompute_node_status', { p_node_id: data.node_id });
  return NextResponse.json({ ok: true });
}
