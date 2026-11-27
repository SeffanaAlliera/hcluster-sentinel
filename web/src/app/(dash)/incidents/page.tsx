import Link from 'next/link';
import { z } from 'zod';
import { requireAdminPage } from '@/lib/auth';
import { listIncidents, listIncidentsPage, lazyEvaluate } from '@/lib/data';
import { INCIDENT_PAGE_SIZE, pageCount, parsePage } from '@/lib/paging';
import { IncidentList } from '@/components/IncidentList';
import { IncidentTable } from '@/components/IncidentTable';
import { Pager } from '@/components/Pager';

type SP = { page?: string; node?: string };

export default async function IncidentsPage({ searchParams }: { searchParams: Promise<SP> }) {
  const sp = await searchParams;
  const { supabase } = await requireAdminPage();
  await lazyEvaluate();
  const nodeId = z.string().uuid().safeParse(sp.node).success ? sp.node : undefined;
  const wanted = parsePage(sp.page);
  const size = INCIDENT_PAGE_SIZE;
  let { rows, total } = await listIncidentsPage(supabase, { nodeId, status: 'resolved', page: wanted, pageSize: size });
  const last = pageCount(total, size);
  const page = Math.min(wanted, last);
  if (page !== wanted) ({ rows } = await listIncidentsPage(supabase, { nodeId, status: 'resolved', page, pageSize: size }));
  const active = await listIncidents(supabase, { nodeId, openOnly: true, limit: 50 });
  const href = (p: number) => `/incidents?${new URLSearchParams({ ...(nodeId ? { node: nodeId } : {}), page: String(p) })}`;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold text-white">Incident history{nodeId && rows[0]?.nodes?.display_name ? ` — ${rows[0].nodes.display_name}` : ''}</h1>
        {nodeId && <Link href="/incidents" className="text-xs text-sky-400 hover:underline">Show all nodes</Link>}
      </div>
      <section><h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Active ({active.length})</h2><div className="card"><IncidentList items={active} showNode={!nodeId} /></div></section>
      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Resolved ({total})</h2>
        <div className="card"><IncidentTable items={rows} showNode={!nodeId} /><Pager page={page} last={last} total={total} size={size} href={href} /></div>
      </section>
    </div>
  );
}
