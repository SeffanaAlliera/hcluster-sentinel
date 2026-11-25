import { requireAdminPage } from '@/lib/auth';
import { listServices, lazyEvaluate } from '@/lib/data';
import { ServiceTable } from '@/components/ServiceTable';

export default async function ServicesPage() {
  const { supabase } = await requireAdminPage();
  await lazyEvaluate();
  const rows = await listServices(supabase);
  const order = { down: 0, degraded: 1, unknown: 2, operational: 3 } as const;
  rows.sort((a, b) => order[a.effective] - order[b.effective] || a.name.localeCompare(b.name));
  return (<div className="space-y-4"><h1 className="text-xl font-semibold text-white">Services</h1><div className="card"><ServiceTable rows={rows} /></div></div>);
}
