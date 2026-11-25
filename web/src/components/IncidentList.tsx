import type { IncidentRow } from '@/lib/status';
import { ago, incidentLength, utc } from '@/lib/format';
import { StatusBadge } from './StatusBadge';

const KIND: Record<IncidentRow['kind'], { down: string; up: string }> = {
  node_offline: { down: 'Node offline', up: 'Node recovered' },
  service_down: { down: 'Service unavailable', up: 'Service recovered' },
  prometheus_unavailable: { down: 'Prometheus unavailable', up: 'Prometheus recovered' },
  metrics_failure: { down: 'Metrics collection failure', up: 'Metrics collection recovered' },
};

export function IncidentList({ items, showNode = true }: { items: IncidentRow[]; showNode?: boolean }) {
  if (!items.length) return <p className="px-4 py-6 text-sm text-slate-400">No incidents recorded. 🎉</p>;
  return (
    <ul className="divide-y divide-ink-700">
      {items.map((i) => {
        const where = [showNode ? i.nodes?.display_name : null, i.services?.name].filter(Boolean).join(' · ');
        return (
          <li key={i.id} className="px-4 py-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={i.resolved_at ? 'operational' : 'down'} label={i.resolved_at ? 'Resolved' : 'Active'} small />
              <span className="font-medium text-slate-100">{KIND[i.kind].down}</span>
              {where && <span className="text-slate-400">— {where}</span>}
            </div>
            <div className="mt-1 text-xs text-slate-400">
              ▼ {KIND[i.kind].down} at <span className="font-mono">{utc(i.started_at)}</span> ({ago(i.started_at)})
              {i.resolved_at && <><br />▲ {KIND[i.kind].up} at <span className="font-mono">{utc(i.resolved_at)}</span> · lasted {incidentLength(i.started_at, i.resolved_at)}{i.resolved_reason ? ` · ${i.resolved_reason}` : ''}</>}
            </div>
            {i.detail && <div className="mt-1 truncate font-mono text-xs text-slate-500" title={i.detail}>{i.detail}</div>}
          </li>
        );
      })}
    </ul>
  );
}
