import { NextResponse, type NextRequest } from 'next/server';
import { z } from 'zod';
import { requireAdminApi } from '@/lib/auth';
import { getNode, listIncidents, listServices } from '@/lib/data';
import { adminClient } from '@/lib/supabase/admin';
import { nodePatch } from '@/lib/validation';
export const dynamic = 'force-dynamic';
type Ctx = { params: Promise<{ id: string }> };
const uuid = z.string().uuid();

export async function GET(req: NextRequest, { params }: Ctx) {
  const a = await requireAdminApi(req);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!uuid.safeParse(id).success) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  const node = await getNode(a.admin.supabase, id);
  if (!node) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  const [services, incidents] = await Promise.all([listServices(a.admin.supabase, id), listIncidents(a.admin.supabase, { nodeId: id, limit: 50 })]);
  return NextResponse.json({ node, services, incidents });
}

export async function PATCH(req: NextRequest, { params }: Ctx) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!uuid.safeParse(id).success) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  const body = nodePatch.safeParse(await req.json().catch(() => null));
  if (!body.success) return NextResponse.json({ error: 'invalid', issues: body.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`) }, { status: 400 });
  const db = adminClient();
  const update: Record<string, unknown> = { ...body.data, updated_at: new Date().toISOString() };
  if (body.data.enabled === false) { update.status = 'unknown'; update.status_reason = 'Disabled'; }
  const { error } = await db.from('nodes').update(update).eq('id', id);
  if (error) return NextResponse.json({ error: /nodes_threshold/.test(error.message) ? 'threshold must be greater than interval' : 'update_failed' }, { status: 400 });
  if (body.data.enabled === false) {
    await db.from('incidents').update({ resolved_at: new Date().toISOString(), resolved_reason: 'node disabled' }).eq('node_id', id).is('resolved_at', null);
  }
  return NextResponse.json({ ok: true });
}

export async function DELETE(req: NextRequest, { params }: Ctx) {
  const a = await requireAdminApi(req, true);
  if ('error' in a) return a.error;
  const { id } = await params;
  if (!uuid.safeParse(id).success) return NextResponse.json({ error: 'not_found' }, { status: 404 });
  const { error } = await adminClient().from('nodes').delete().eq('id', id);   // cascades: credentials, services, metrics, incidents
  return error ? NextResponse.json({ error: 'delete_failed' }, { status: 500 }) : NextResponse.json({ ok: true });
}
