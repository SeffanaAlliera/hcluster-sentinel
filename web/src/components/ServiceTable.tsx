import Link from 'next/link';
import type { Health, ServiceRow } from '@/lib/status';
import { ago } from '@/lib/format';
import { StatusBadge } from './StatusBadge';
import { ServiceActions } from './ServiceActions';

export type ServiceView = ServiceRow & { node_name: string; effective: Health };

export function ServiceTable({ rows, showNode = true, canEdit = false }: { rows: ServiceView[]; showNode?: boolean; canEdit?: boolean }) {
  if (!rows.length) return <p className="px-4 py-6 text-sm text-slate-400">No services reported yet. Services appear automatically once the agent sends its first report.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-slate-400">
          <tr><th className="px-4 py-2">Service</th>{showNode && <th className="px-4 py-2">Node</th>}<th className="px-4 py-2">Status</th><th className="px-4 py-2">Response</th><th className="px-4 py-2">Last success</th><th className="px-4 py-2">Last error</th>{canEdit && <th className="px-4 py-2" />}</tr>
        </thead>
        <tbody className="divide-y divide-ink-700">
          {rows.map((s) => (
            <tr key={s.id} className={s.enabled ? '' : 'opacity-50'}>
              <td className="px-4 py-2"><div className="font-medium text-slate-100">{s.name}</div><div className="max-w-[18rem] truncate font-mono text-xs text-slate-500" title={s.target ?? ''}>{s.check_type.toUpperCase()} {s.target ?? ''}</div></td>
              {showNode && <td className="px-4 py-2"><Link className="text-sky-300 hover:underline" href={`/nodes/${s.node_id}`}>{s.node_name}</Link></td>}
              <td className="px-4 py-2"><StatusBadge status={s.enabled ? s.effective : 'disabled'} label={s.enabled ? undefined : 'Muted'} /></td>
              <td className="px-4 py-2 font-mono text-xs">{s.effective === 'unknown' || s.response_ms == null ? '—' : `${s.response_ms} ms`}</td>
              <td className="px-4 py-2 text-xs">{ago(s.last_success_at)}</td>
              <td className="max-w-[18rem] px-4 py-2 text-xs text-slate-400"><span className="block truncate" title={s.last_error ?? ''}>{s.last_error ?? '—'}</span></td>
              {canEdit && <td className="px-4 py-2 text-right"><ServiceActions id={s.id} enabled={s.enabled} /></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
