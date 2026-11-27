import Link from 'next/link';
import { notFound } from 'next/navigation';
import { z } from 'zod';
import { requireAdminPage } from '@/lib/auth';
import { getNode, listIncidentsPage, listServices, metricSeries } from '@/lib/data';
import { NODE_PREVIEW } from '@/lib/paging';
import { effectiveNodeStatus } from '@/lib/status';
import { ago, bytes, duration, fmtTime, pct, tzLabel } from '@/lib/format';
import { RANGES, parseRange } from '@/lib/validation';
import { drives, nics, pick, seriesKey, worstDrive, type LatestRow } from '@/lib/metrics';
import { StatusBadge } from '@/components/StatusBadge';
import { ServiceTable } from '@/components/ServiceTable';
import { IncidentList } from '@/components/IncidentList';
import { cloneElement, type ReactElement } from 'react';
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
    listServices(supabase, id), listIncidentsPage(supabase, { nodeId: id, status: 'all', page: 1, pageSize: NODE_PREVIEW }), metricSeries(supabase, id, range),
    supabase.from('node_latest_metrics').select('name,label,value,source,ts').eq('node_id', id),
  ]);
  const latest = (latestRes.data ?? []) as LatestRow[];
  const driveList = drives(latest);
  const worst = worstDrive(latest);
  const nicList = nics(latest);
  const temp = pick(latest, 'cpu_temp_celsius');
  const eff = node.enabled ? effectiveNodeStatus(node) : 'unknown';
  const live = node.enabled && (eff === 'online' || eff === 'degraded');

  // merge series into chart rows. Keys are k0,k1,... (labels may contain characters recharts treats as paths)
  type Row = Record<string, number | null> & { t: number };
  const build = (defs: { name: string; label: string; legend: string; color: string }[]) => {
    const m = new Map<number, Row>();
    const series = defs.map((d, i) => ({ key: `k${i}`, label: d.legend, color: d.color, pts: ms.series[seriesKey(d.name, d.label)] ?? [] }));
    for (const sr of series) for (const [t, v] of sr.pts) { const r = m.get(t) ?? ({ t } as Row); r[sr.key] = v; m.set(t, r); }
    return { data: [...m.values()].sort((x, y) => x.t - y.t), series: series.filter((x) => x.pts.length).map(({ key, label, color }) => ({ key, label, color })) };
  };
  const COLORS = ['#fbbf24', '#38bdf8', '#a78bfa', '#34d399', '#f472b6', '#fb923c', '#22d3ee', '#e879f9', '#a3e635', '#f87171', '#2dd4bf', '#818cf8', '#facc15', '#94a3b8'];
  const currentMounts = new Set(driveList.map((d) => d.mount));      // hide drives that are no longer reported (e.g. old /boot/efi history)
  const driveMounts = [...new Set(Object.keys(ms.series).filter((k) => k.startsWith('disk_percent|')).map((k) => k.slice('disk_percent|'.length)))]
    .filter((m) => currentMounts.size === 0 || currentMounts.has(m)).sort();
  const cpuC = build([{ name: 'cpu_percent', label: '', legend: 'CPU', color: '#38bdf8' }]);
  const tempC = build([{ name: 'cpu_temp_celsius', label: '', legend: 'CPU temp', color: '#fb923c' }]);
  const memC = build([{ name: 'mem_percent', label: '', legend: 'Memory', color: '#a78bfa' }]);
  const sizeOf = (m: string) => driveList.find((d) => d.mount === m)?.total;
  const diskC = build(driveMounts.map((m, i) => ({ name: 'disk_percent', label: m, legend: sizeOf(m) ? `${m} · ${bytes(sizeOf(m))}` : m, color: COLORS[i % COLORS.length] })));
  const ioMounts = [...new Set(Object.keys(ms.series).filter((k) => k.startsWith('disk_read_bytes_per_sec|') || k.startsWith('disk_write_bytes_per_sec|')).map((k) => k.slice(k.indexOf('|') + 1)))]
    .filter((m) => currentMounts.size === 0 || currentMounts.has(m)).sort();
  const readC = build(ioMounts.map((m, i) => ({ name: 'disk_read_bytes_per_sec', label: m, legend: m, color: COLORS[i % COLORS.length] })));
  const writeC = build(ioMounts.map((m, i) => ({ name: 'disk_write_bytes_per_sec', label: m, legend: m, color: COLORS[i % COLORS.length] })));
  const cpuName = node.cpu_model ? `${node.cpu_model}${node.cpu_cores ? ` · ${node.cpu_cores} threads` : ''}` : undefined;
  const ramTotal = pick(latest, 'mem_total_bytes');
  const ramName = ramTotal != null ? `Total RAM ${bytes(ramTotal)}` : undefined;
  // GPUs (auto-detected by the agent; nothing is shown for a node without one)
  const gpuLabels = [...new Set(Object.keys(ms.series).filter((k) => /^gpu_(utilization_percent|clock_mhz|vram_percent)\|/.test(k)).map((k) => k.slice(k.indexOf('|') + 1)))].sort();
  const gpuSeries = (name: string) => build(gpuLabels.filter((l) => ms.series[`${name}|${l}`]).map((l, i) => ({ name, label: l, legend: l, color: COLORS[i % COLORS.length] })));
  const gpuUtilC = gpuSeries('gpu_utilization_percent'), gpuClockC = gpuSeries('gpu_clock_mhz'), gpuVramC = gpuSeries('gpu_vram_percent');
  const vramTotals = latest.filter((r) => r.name === 'gpu_vram_total_bytes').map((r) => `${r.label || 'GPU'} ${bytes(r.value)}`).join(' · ');
  const gpuIntegrated = gpuLabels.some((l) => /\(i915\)|\(xe\)/.test(l));

  const netC = build([{ name: 'net_rx_bytes_per_sec', label: '', legend: 'Receive (all NICs)', color: '#34d399' }, { name: 'net_tx_bytes_per_sec', label: '', legend: 'Transmit (all NICs)', color: '#f472b6' }]);
  const nicNames = [...new Set(Object.keys(ms.series).filter((k) => k.startsWith('net_rx_bytes_per_sec|')).map((k) => k.slice('net_rx_bytes_per_sec|'.length)))].sort();
  const nicCharts = nicNames.map((n) => ({ n, c: build([{ name: 'net_rx_bytes_per_sec', label: n, legend: 'Receive', color: '#34d399' }, { name: 'net_tx_bytes_per_sec', label: n, legend: 'Transmit', color: '#f472b6' }]) }));
  const sources = new Set(latest.map((l) => l.source));

  const tzp = node.timezone;
  const span = RANGES[range].seconds * 1000;
  const charts: ReactElement<{ wide?: boolean }>[] = [
    <MetricChart key="cpu" title="CPU utilisation" subtitle={cpuName} unit="percent" spanMs={span} tz={tzp} {...cpuC} />,
    <MetricChart key="temp" title="CPU temperature" subtitle={cpuName} unit="celsius" spanMs={span} tz={tzp} {...tempC} />,
    <MetricChart key="mem" title="Memory utilisation" subtitle={ramName} unit="percent" spanMs={span} tz={tzp} {...memC} />,
    <MetricChart key="disk" title={`Disk utilisation${driveMounts.length > 1 ? ` (${driveMounts.length} drives)` : ''}`} unit="percent" spanMs={span} tz={tzp} {...diskC} />,
    <MetricChart key="rd" title="Disk read throughput (per drive)" unit="bytes" spanMs={span} tz={tzp} {...readC} />,
    <MetricChart key="wr" title="Disk write throughput (per drive)" unit="bytes" spanMs={span} tz={tzp} {...writeC} />,
    <MetricChart key="net" title="Network throughput — all NICs combined" unit="bytes" spanMs={span} tz={tzp} {...netC} />,
    ...(nicCharts.length > 1 ? nicCharts.map(({ n, c }) => <MetricChart key={`nic-${n}`} title={`Network — ${n}`} unit="bytes" spanMs={span} tz={tzp} {...c} />) : []),
    ...(gpuUtilC.series.length ? [<MetricChart key="gpu-util" title="GPU utilisation" subtitle={gpuIntegrated ? 'Integrated GPU: busiest engine (render / video / blitter)' : undefined} unit="percent" spanMs={span} tz={tzp} {...gpuUtilC} />] : []),
    ...(gpuClockC.series.length ? [<MetricChart key="gpu-clock" title="GPU clock speed" unit="mhz" spanMs={span} tz={tzp} {...gpuClockC} />] : []),
    ...(gpuVramC.series.length ? [<MetricChart key="gpu-vram" title="GPU VRAM used" subtitle={vramTotals || undefined} unit="percent" spanMs={span} tz={tzp} {...gpuVramC} />] : []),
  ];
  if (charts.length % 2 === 1) charts[charts.length - 1] = cloneElement(charts[charts.length - 1], { wide: true });   // keep the two-column grid balanced
  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-xs text-slate-400 hover:text-slate-200">← All nodes</Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-white">{node.display_name}</h1>
          <StatusBadge status={node.enabled ? eff : 'disabled'} />
          <span className="font-mono text-xs text-slate-500">{node.hostname}</span>
        </div>
        <p className="mt-1 text-xs text-slate-500">All times on this page are in the node's own timezone: {node.timezone ? tzLabel(node.timezone) : 'unknown, showing UTC'}.</p>
        {node.enabled && node.status_reason && eff !== 'online' && <p className="mt-1 text-sm text-slate-400">{node.status_reason}</p>}
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="Last heartbeat" value={ago(node.last_heartbeat_at)} />
        <StatCard label="Uptime" value={live ? duration(node.uptime_seconds) : '—'} />
        <StatCard label="CPU / RAM" value={live ? `${pct(pick(latest, 'cpu_percent'))} / ${pct(pick(latest, 'mem_percent'))}` : '—'} />
        <StatCard label="CPU temperature" value={live && temp != null ? `${temp.toFixed(0)}°C` : 'n/a'} tone={live && temp != null && temp >= 85 ? 'bad' : live && temp != null && temp >= 75 ? 'warn' : undefined} />
        <StatCard label={driveList.length > 1 ? `Fullest drive (${worst?.mount})` : 'Disk'} value={live && worst ? `${pct(worst.percent)}${worst.available != null ? ` · ${bytes(worst.available)} free` : ''}` : '—'} />
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
        <div className="grid gap-4 lg:grid-cols-2">{charts}</div>
        {driveList.length > 0 && (
          <div className="card mt-4 p-4">
            <h3 className="mb-3 text-sm font-medium text-slate-200">Drives ({driveList.length})</h3>
            <ul className="space-y-3">
              {driveList.map((d) => {
                const v = d.percent ?? 0; const color = v >= 90 ? 'bg-rose-500' : v >= 75 ? 'bg-amber-400' : 'bg-sky-500';
                return (<li key={d.mount}>
                  <div className="mb-1 flex justify-between text-xs"><span className="font-mono text-slate-200">{d.mount}</span><span className="text-slate-400">{pct(d.percent)} used · {bytes(d.available)} free of {bytes(d.total)}</span></div>
                  <div className="h-1.5 rounded-full bg-ink-700"><div className={`h-1.5 rounded-full ${color}`} style={{ width: `${Math.min(100, v)}%` }} /></div></li>);
              })}
            </ul>
            {nicList.length > 0 && <p className="mt-3 text-xs text-slate-500">Network interfaces reporting: {nicList.join(', ')}</p>}
          </div>
        )}
      </section>

      <section><h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Services</h2><div className="card"><ServiceTable rows={services} showNode={false} canEdit /></div></section>
      <section>
        <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Latest incidents</h2>
          {incidents.total > NODE_PREVIEW && <Link href={`/incidents?node=${id}`} className="text-xs text-sky-400 hover:underline">View all {incidents.total} →</Link>}</div>
        <div className="card"><IncidentList items={incidents.rows} showNode={false} /></div>
      </section>

      <section className="card space-y-3 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Node details</h2>
        <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          {([['Node ID', node.id], ['OS', node.os ?? '—'], ['Architecture', node.arch ?? '—'], ['Kernel', node.kernel ?? '—'], ['Agent', node.agent_version ?? '—'], ['Registered', fmtTime(node.created_at, node.timezone)], ['Heartbeat / offline after', `${node.heartbeat_interval_seconds}s / ${node.offline_threshold_seconds}s`], ['Last heartbeat', fmtTime(node.last_heartbeat_at, node.timezone)], ['Node timezone', node.timezone ? tzLabel(node.timezone) : 'unknown (showing UTC)']] as const).map(([k, v]) => (
            <div key={k}><dt className="text-xs text-slate-500">{k}</dt><dd className="break-all font-mono text-xs text-slate-200">{v}</dd></div>))}
        </dl>
        <NodeActions id={node.id} enabled={node.enabled} hb={node.heartbeat_interval_seconds} off={node.offline_threshold_seconds} />
      </section>
    </div>
  );
}
