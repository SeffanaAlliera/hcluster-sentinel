import 'server-only';
import type { SupabaseClient } from '@supabase/supabase-js';
import { adminClient } from '@/lib/supabase/admin';
import { RANGES, type RangeKey } from '@/lib/validation';
import { seriesKey, type LatestRow } from '@/lib/metrics';
import { effectiveNodeStatus, effectiveServiceStatus, type IncidentRow, type NodeRow, type NodeStatus, type ServiceRow } from '@/lib/status';

export interface NodeView extends NodeRow { effective: NodeStatus; services_ok: number; services_total: number; latest: LatestRow[] }

/** Offline detection is also run lazily so incidents exist even without pg_cron. Never throws. */
export async function lazyEvaluate() {
  try { await adminClient().rpc('evaluate_nodes'); } catch { /* ignore */ }
}

export async function listNodes(sb: SupabaseClient): Promise<NodeView[]> {
  const [nodes, services, latest] = await Promise.all([
    sb.from('nodes').select('*').order('display_name'),
    sb.from('services').select('id,node_id,status,enabled'),
    sb.from('node_latest_metrics').select('node_id,name,label,value,source,ts'),
  ]);
  if (nodes.error) throw nodes.error;
  const lm: Record<string, LatestRow[]> = {};
  for (const r of latest.data ?? []) (lm[r.node_id] ??= []).push({ name: r.name, label: r.label, value: r.value, source: r.source, ts: r.ts });
  return (nodes.data as NodeRow[]).map((n) => {
    const eff = effectiveNodeStatus(n);
    const svcs = (services.data ?? []).filter((s) => s.node_id === n.id && s.enabled);
    return {
      ...n, effective: eff, latest: lm[n.id] ?? [],
      services_total: svcs.length,
      services_ok: svcs.filter((s) => effectiveServiceStatus(s, eff) === 'operational').length,
    };
  });
}

export async function getNode(sb: SupabaseClient, id: string) {
  const { data, error } = await sb.from('nodes').select('*').eq('id', id).maybeSingle();
  if (error) throw error;
  return data as NodeRow | null;
}

export async function listServices(sb: SupabaseClient, nodeId?: string) {
  let q = sb.from('services').select('*, nodes(display_name,enabled,status,last_heartbeat_at,offline_threshold_seconds)').order('name');
  if (nodeId) q = q.eq('node_id', nodeId);
  const { data, error } = await q;
  if (error) throw error;
  type Row = ServiceRow & { nodes: Pick<NodeRow, 'display_name' | 'enabled' | 'status' | 'last_heartbeat_at' | 'offline_threshold_seconds'> };
  return (data as unknown as Row[]).map((s) => ({ ...s, node_name: s.nodes.display_name, effective: effectiveServiceStatus(s, effectiveNodeStatus(s.nodes)) }));
}

export async function listIncidents(sb: SupabaseClient, opts: { nodeId?: string; openOnly?: boolean; limit?: number } = {}) {
  let q = sb.from('incidents').select('*, nodes(display_name,timezone), services(name)').order('started_at', { ascending: false }).limit(opts.limit ?? 100);
  if (opts.nodeId) q = q.eq('node_id', opts.nodeId);
  if (opts.openOnly) q = q.is('resolved_at', null);
  const { data, error } = await q;
  if (error) throw error;
  return data as unknown as IncidentRow[];
}

export async function overview(sb: SupabaseClient) {
  await lazyEvaluate();
  const [nodes, services, incidents, open] = await Promise.all([
    listNodes(sb), sb.from('services').select('id,enabled'), listIncidents(sb, { limit: 8 }), listIncidents(sb, { openOnly: true, limit: 500 }),
  ]);
  const count = (s: NodeStatus) => nodes.filter((n) => n.enabled && n.effective === s).length;
  return {
    totals: { nodes: nodes.length, online: count('online'), offline: count('offline'), degraded: count('degraded'), unknown: nodes.filter((n) => !n.enabled || n.effective === 'unknown').length,
              services: (services.data ?? []).filter((s) => s.enabled).length, active_incidents: open.length },
    nodes, recent_incidents: incidents,
  };
}

export const SERIES_METRICS = ['cpu_percent', 'cpu_temp_celsius', 'mem_percent', 'disk_percent', 'net_rx_bytes_per_sec', 'net_tx_bytes_per_sec', 'disk_read_bytes_per_sec', 'disk_write_bytes_per_sec'] as const;
const PAGE = 1000;   // PostgREST returns at most 1000 rows per request; labeled series can exceed that on 7d ranges

export async function metricSeries(sb: SupabaseClient, nodeId: string, range: RangeKey) {
  const { seconds, bucket } = RANGES[range];
  const to = new Date(); const from = new Date(to.getTime() - seconds * 1000);
  const series: Record<string, [number, number][]> = {};
  for (let page = 0; page < 10; page++) {
    const { data, error } = await sb.rpc('metric_series', {
      p_node_id: nodeId, p_names: [...SERIES_METRICS], p_from: from.toISOString(), p_to: to.toISOString(), p_bucket_seconds: bucket,
    }).range(page * PAGE, page * PAGE + PAGE - 1);
    if (error) throw error;
    const rows = (data ?? []) as { bucket: string; name: string; label: string; value: number }[];
    for (const r of rows) (series[seriesKey(r.name, r.label)] ??= []).push([Date.parse(r.bucket), r.value]);
    if (rows.length < PAGE) break;
  }
  return { range, from: from.toISOString(), to: to.toISOString(), bucket_seconds: bucket, series };
}
