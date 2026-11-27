import type { IncidentRow } from '@/lib/status';
import { fmtTime, incidentLength } from '@/lib/format';
import { StatusBadge } from './StatusBadge';

const TITLE: Record<IncidentRow['kind'], string> = {
  node_offline: 'Node offline', service_down: 'Service unavailable',
  prometheus_unavailable: 'Prometheus unavailable', metrics_failure: 'Metrics collection failure',
};

export function IncidentTable({ items, showNode = true }: { items: IncidentRow[]; showNode?: boolean }) {
  if (!items.length) return <p className="px-4 py-6 text-sm text-slate-400">No incidents match.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-slate-400">
          <tr className="border-b border-ink-700">
            <th className="px-4 py-2 font-medium">Status</th><th className="px-4 py-2 font-medium">Incident</th>
            {showNode && <th className="px-4 py-2 font-medium">Node</th>}
            <th className="px-4 py-2 font-medium">Started</th><th className="px-4 py-2 font-medium">Recovered</th>
            <th className="px-4 py-2 font-medium">Duration</th><th className="px-4 py-2 font-medium">Detail</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-700">
          {items.map((i) => (
            <tr key={i.id} className="align-top">
              <td className="px-4 py-2"><StatusBadge status={i.resolved_at ? 'operational' : 'down'} label={i.resolved_at ? 'Resolved' : 'Active'} small /></td>
              <td className="px-4 py-2 text-slate-100">{TITLE[i.kind]}{i.services?.name && <span className="text-slate-400"> — {i.services.name}</span>}</td>
              {showNode && <td className="px-4 py-2 text-slate-300">{i.nodes?.display_name ?? '—'}</td>}
              <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-slate-300">{fmtTime(i.started_at, i.nodes?.timezone)}</td>
              <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-slate-300">{i.resolved_at ? fmtTime(i.resolved_at, i.nodes?.timezone) : '—'}</td>
              <td className="whitespace-nowrap px-4 py-2 text-xs text-slate-300">{incidentLength(i.started_at, i.resolved_at)}</td>
              <td className="max-w-[260px] truncate px-4 py-2 font-mono text-xs text-slate-500" title={i.detail ?? ''}>{i.detail ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
