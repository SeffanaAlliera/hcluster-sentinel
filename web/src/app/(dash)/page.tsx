import Link from 'next/link';
import { requireAdminPage } from '@/lib/auth';
import { overview } from '@/lib/data';
import { NodeCard } from '@/components/NodeCard';
import { StatCard } from '@/components/StatCard';
import { IncidentList } from '@/components/IncidentList';

export default async function Overview() {
  const { supabase } = await requireAdminPage();
  const o = await overview(supabase);
  const t = o.totals;
  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
        <StatCard label="Nodes" value={t.nodes} />
        <StatCard label="Online" value={t.online} tone="good" />
        <StatCard label="Offline" value={t.offline} tone={t.offline ? 'bad' : undefined} />
        <StatCard label="Degraded" value={t.degraded} tone={t.degraded ? 'warn' : undefined} />
        <StatCard label="Unknown / disabled" value={t.unknown} />
        <StatCard label="Services" value={t.services} />
        <StatCard label="Active incidents" value={t.active_incidents} tone={t.active_incidents ? 'bad' : 'good'} />
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Nodes</h2><Link href="/nodes/new" className="btn btn-primary">+ Add node</Link></div>
        {o.nodes.length === 0 ? (
          <div className="card p-8 text-center text-slate-400">No nodes yet. <Link href="/nodes/new" className="text-sky-300 underline">Add your first node</Link> to get an agent token.</div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">{o.nodes.map((n) => <NodeCard key={n.id} n={n} />)}</div>
        )}
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Recent incidents</h2><Link href="/incidents" className="text-sm text-sky-300 hover:underline">View all</Link></div>
        <div className="card"><IncidentList items={o.recent_incidents} /></div>
      </section>
    </div>
  );
}
