import { requireAdminPage } from '@/lib/auth';
import { listIncidents, lazyEvaluate } from '@/lib/data';
import { IncidentList } from '@/components/IncidentList';

export default async function IncidentsPage() {
  const { supabase } = await requireAdminPage();
  await lazyEvaluate();
  const items = await listIncidents(supabase, { limit: 200 });
  const active = items.filter((i) => !i.resolved_at);
  const past = items.filter((i) => i.resolved_at);
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-white">Incident history</h1>
      <section><h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Active ({active.length})</h2><div className="card"><IncidentList items={active} /></div></section>
      <section><h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-400">Resolved</h2><div className="card"><IncidentList items={past} /></div></section>
    </div>
  );
}
