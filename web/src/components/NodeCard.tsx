import Link from 'next/link';
import type { NodeView } from '@/lib/data';
import { ago, duration, pct } from '@/lib/format';
import { drives, pick, worstDrive } from '@/lib/metrics';
import { StatusBadge } from './StatusBadge';

function Bar({ label, value, extra }: { label: string; value: number | undefined; extra?: string }) {
  const v = value == null ? null : Math.max(0, Math.min(100, value));
  const color = v == null ? 'bg-slate-600' : v >= 90 ? 'bg-rose-500' : v >= 75 ? 'bg-amber-400' : 'bg-sky-500';
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-slate-400"><span>{label}</span><span className="font-mono text-slate-200">{v == null ? 'n/a' : pct(v)}{extra ? ` · ${extra}` : ''}</span></div>
      <div className="h-1.5 rounded-full bg-ink-700" role="progressbar" aria-label={label} aria-valuenow={v ?? undefined} aria-valuemin={0} aria-valuemax={100}>
        <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${v ?? 0}%` }} />
      </div>
    </div>
  );
}

export function NodeCard({ n }: { n: NodeView }) {
  const status = n.enabled ? n.effective : 'disabled';
  const stale = n.effective === 'offline' || n.effective === 'unknown' || !n.enabled;
  const m = (k: string) => (stale ? undefined : pick(n.latest, k));
  const temp = m('cpu_temp_celsius');
  const worst = stale ? null : worstDrive(n.latest);
  const nDrives = drives(n.latest).length;
  return (
    <Link href={`/nodes/${n.id}`} className="card block p-4 transition hover:border-sky-500/50">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0"><div className="truncate text-base font-semibold text-white">{n.display_name}</div><div className="truncate font-mono text-xs text-slate-500">{n.hostname}</div></div>
        <StatusBadge status={status} />
      </div>
      {n.status_reason && n.enabled && n.effective === 'degraded' && <p className="mt-2 text-xs text-amber-300/90">{n.status_reason}</p>}
      <div className="mt-4 space-y-3">
        <Bar label="CPU" value={m('cpu_percent')} extra={temp != null ? `${temp.toFixed(0)}°C` : undefined} />
        <Bar label="RAM" value={m('mem_percent')} />
        <Bar label={nDrives > 1 && worst ? `Disk · fullest of ${nDrives}: ${worst.mount}` : 'Disk'} value={worst?.percent ?? undefined} />
      </div>
      <dl className="mt-4 grid grid-cols-3 gap-2 border-t border-ink-700 pt-3 text-xs">
        <div><dt className="text-slate-500">Last heartbeat</dt><dd className="text-slate-200">{ago(n.last_heartbeat_at)}</dd></div>
        <div><dt className="text-slate-500">Uptime</dt><dd className="text-slate-200">{n.effective === 'offline' || !n.enabled ? '—' : duration(n.uptime_seconds)}</dd></div>
        <div><dt className="text-slate-500">Services</dt><dd className="text-slate-200">{n.services_total ? `${n.services_ok}/${n.services_total} up` : '—'}</dd></div>
      </dl>
    </Link>
  );
}
