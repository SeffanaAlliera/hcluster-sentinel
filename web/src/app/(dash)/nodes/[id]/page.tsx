import Link from 'next/link';
import { notFound } from 'next/navigation';
import { z } from 'zod';
import { requireAdminPage } from '@/lib/auth';
import { getNode, listIncidents, listServices, metricSeries } from '@/lib/data';
import { effectiveNodeStatus } from '@/lib/status';
import { ago, bytes, duration, pct, utc } from '@/lib/format';
import { RANGES, parseRange } from '@/lib/validation';
import { StatusBadge } from '@/components/StatusBadge';
import { ServiceTable } from '@/components/ServiceTable';
import { IncidentList } from '@/components/IncidentList';
import { MetricChart } from '@/components/MetricChart';
import { RangeTabs } from '@/components/RangeTabs';
import { NodeActions } from '@/components/NodeActions';
import { StatCard } from '@/components/StatCard';

export default async function NodePage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ range?: string }> }) {
  const { id } = await params;
  if (!z.string().uuid().safeParse(id).success) notFound();
  const { supabase } = await requireAdminPage();
  const range = parseRange((await searchParams).range);
  const node = await getNode(supabase, id);
  if (!node) notFound();
  const [services, incidents, ms, latestRes] = await Promise.all([
    listServices(supabase, id), listIncidents(supabase, { nodeId: id, limit: 30 }), metricSeries(supabase, id, range),
    supabase.from('node_latest_metrics').select('name,value,source').eq('node_id', id),
  ]);
  const latest = Object.fromEntries((latestRes.data ?? []).map((r) => [r.name, r]));
  const eff = node.enabled ? effectiveNodeStatus(node) : 'unknown';
  const live = node.enabled && (eff === 'online' || eff === 'degraded');

  // merge series into rows keyed by timestamp
  const rows = (keys: string[]) => {
    const m = new Map<number, Record<string, number | null> & { t: number }>();
    for (const k of keys) for (const [t, v] of ms.series[k] ?? []) { const r = m.get(t) ?? ({ t } as Record<string, number | null> & { t: number }); r[k] = v; m.set(t, r); }
    return [...m.values()].sort((a, b) => a.t - b.t);
  };
  const span = RANGES[range].seconds * 1000;
  const sources = new Set((latestRes.data ?? []).map((l) => l.source as string));

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-xs text-slate-400 hover:text-slate-200">← All nodes</Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-white">{node.display_name}</h1>
          <StatusBadge status={node.enabled ? eff : 'disabled'} />
          <span className="font-mono text-xs text-slate-500">{node.hostname}</span>
        </div>
        {node.enabled && node.status_reason && eff !== 'online' && <p className="mt-1 text-sm text-slate-400">{node.status_reason}</p>}
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Last heartbeat" value={ago(node.last_heartbeat_at)} />
        <StatCard label="Uptime" value={live ? duration(node.uptime_seconds) : '—'} />
        <StatCard label="CPU / RAM" value={live ? `${pct(latest.cpu_percent?.value)} / ${pct(latest.mem_percent?.value)}` : '—'} />
        <StatCard label="Disk" value={live ? `${pct(latest.disk_percent?.value)}${latest.disk_available_bytes ? ` · ${bytes(latest.disk_available_bytes.value)} free` : ''}` : '—'} />
      </section>

      <section className="card p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Availability layers</h2>
        <dl className="grid gap-4 text-sm md:grid-cols-4">
          <div><dt className="text-slate-500">Node</dt><dd className="mt-1"><StatusBadge status={node.enabled ? eff : 'disabled'} /></dd></div>
          <div><dt className="text-slate-500">Services</dt><dd className="mt-1">{services.length ? `${services.filter((s) => s.enabled && s.effective === 'operational').length}/${services.filter((s) => s.enabled).length} operational` : 'none reported'}</dd></div>
          <div><dt className="text-slate-500">Prometheus</dt><dd className="mt-1">
            <StatusBadge status={node.prometheus_status === 'ok' ? 'operational' : node.prometheus_status === 'unreachable' ? 'degraded' : 'unknown'} label={{ ok: 'Reachable', unreachable: 'Unreachable', disabled: 'Disabled', unknown: 'Unknown' }[node.prometheus_status]} />
            {node.prometheus_targets_total != null && <span className="ml-2 text-xs text-slate-400">{node.prometheus_targets_up}/{node.prometheus_targets_total} targets up</span>}
            {node.prometheus_error && <div className="mt-1 truncate text-xs text-slate-500" title={node.prometheus_error}>{node.prometheus_error}</div>}</dd></div>
          <div><dt className="text-slate-500">Metrics collection</dt><dd className="mt-1">
            <StatusBadge status={{ ok: 'operational', partial: 'degraded', failed: 'down', disabled: 'unknown', unknown: 'unknown' }[node.metrics_status] ?? 'unknown'} label={{ ok: 'OK', partial: 'Partial', failed: 'Failing', disabled: 'Disabled', unknown: 'Unknown' }[node.metrics_status]} />
            {sources.has('local') && <div className="mt-1 text-xs text-slate-500">some values from local /proc fallback</div>}
            {node.metrics_unavailable.length > 0 && <div className="mt-1 text-xs text-slate-500">unavailable: {node.metrics_unavailable.join(', ')}</div>}</dd></div>
        </dl>
        {node.prometheus_down_targets.length > 0 && <p className="mt-3 text-xs text-amber-300/90">Targets down: {node.prometheus_down_targets.join(', ')}</p>}
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Metrics</h2><RangeTabs base={`/nodes/${id}`} current={range} /></div>
        <div className="grid gap-4 lg:grid-cols-2">
          <MetricChart title="CPU utilisation" unit="percent" spanMs={span} data={rows(['cpu_percent'])} series={[{ key: 'cpu_percent', label: 'CPU', color: '#38bdf8' }]} />
          <MetricChart title="Memory utilisation" unit="percent" spanMs={span} data={rows(['mem_percent'])} series={[{ key: 'mem_percent', label: 'Memory', color: '#a78bfa' }]} />
          <MetricChart title="Disk utilisation" unit="percent" spanMs={span} data={rows(['disk_percent'])} series={[{ key: 'disk_percent', label: 'Disk', color: '#fbbf24' }]} />
          <MetricChart title="Network throughput" unit="bytes" spanMs={span} data={rows(['net_rx_bytes_per_sec', 'net_tx_bytes_per_sec'])} series={[{ key: 'net_rx_bytes_per_sec', label: 'Receive', color: '#34d399' }, { key: 'net_tx_bytes_per_sec', label: 'Transmit', color: '#f472b6' }]} />
        </div>
      </section>

      <section><h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Services</h2><div className="card"><ServiceTable rows={services} showNode={false} canEdit /></div></section>
      <section><h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Incidents</h2><div className="card"><IncidentList items={incidents} showNode={false} /></div></section>

      <section className="card space-y-3 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Node details</h2>
        <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          {([['Node ID', node.id], ['OS', node.os ?? '—'], ['Architecture', node.arch ?? '—'], ['Kernel', node.kernel ?? '—'], ['Agent', node.agent_version ?? '—'], ['Registered', utc(node.created_at)], ['Heartbeat / offline after', `${node.heartbeat_interval_seconds}s / ${node.offline_threshold_seconds}s`], ['Last heartbeat', utc(node.last_heartbeat_at)]] as const).map(([k, v]) => (
            <div key={k}><dt className="text-xs text-slate-500">{k}</dt><dd className="break-all font-mono text-xs text-slate-200">{v}</dd></div>))}
        </dl>
        <NodeActions id={node.id} enabled={node.enabled} hb={node.heartbeat_interval_seconds} off={node.offline_threshold_seconds} />
      </section>
    </div>
  );
}
