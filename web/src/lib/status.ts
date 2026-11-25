export type NodeStatus = 'online' | 'offline' | 'degraded' | 'unknown';
export type Health = 'operational' | 'degraded' | 'down' | 'unknown';

export interface NodeRow {
  id: string; hostname: string; display_name: string; os: string | null; arch: string | null; kernel: string | null;
  agent_version: string | null; enabled: boolean; status: NodeStatus; status_reason: string | null;
  heartbeat_interval_seconds: number; offline_threshold_seconds: number; last_heartbeat_at: string | null;
  uptime_seconds: number | null; prometheus_status: string; prometheus_error: string | null;
  prometheus_targets_up: number | null; prometheus_targets_total: number | null; prometheus_down_targets: string[];
  metrics_status: string; metrics_unavailable: string[]; created_at: string;
}
export interface ServiceRow {
  id: string; node_id: string; key: string; name: string; check_type: 'http' | 'tcp'; target: string | null;
  enabled: boolean; status: Health; response_ms: number | null; http_status: number | null;
  last_checked_at: string | null; last_success_at: string | null; last_error: string | null;
}
export interface IncidentRow {
  id: string; node_id: string; service_id: string | null;
  kind: 'node_offline' | 'service_down' | 'prometheus_unavailable' | 'metrics_failure';
  title: string; detail: string | null; started_at: string; resolved_at: string | null; resolved_reason: string | null;
  nodes?: { display_name: string } | null; services?: { name: string } | null;
}

/** DB status can lag by up to a cron tick; compute the truth from the heartbeat age too. */
export function effectiveNodeStatus(n: Pick<NodeRow, 'enabled' | 'status' | 'last_heartbeat_at' | 'offline_threshold_seconds'>, now = Date.now()): NodeStatus {
  if (!n.enabled || !n.last_heartbeat_at) return 'unknown';
  const age = (now - Date.parse(n.last_heartbeat_at)) / 1000;
  if (age > n.offline_threshold_seconds) return 'offline';
  return n.status === 'offline' ? 'online' : n.status;
}
export function effectiveServiceStatus(s: Pick<ServiceRow, 'status'>, node: NodeStatus): Health {
  return node === 'offline' || node === 'unknown' ? 'unknown' : s.status;
}

export const STATUS_STYLE: Record<string, { label: string; dot: string; text: string; ring: string }> = {
  online: { label: 'Online', dot: 'bg-emerald-400', text: 'text-emerald-300', ring: 'ring-emerald-500/30 bg-emerald-500/10' },
  operational: { label: 'Operational', dot: 'bg-emerald-400', text: 'text-emerald-300', ring: 'ring-emerald-500/30 bg-emerald-500/10' },
  offline: { label: 'Offline', dot: 'bg-rose-500', text: 'text-rose-300', ring: 'ring-rose-500/30 bg-rose-500/10' },
  down: { label: 'Down', dot: 'bg-rose-500', text: 'text-rose-300', ring: 'ring-rose-500/30 bg-rose-500/10' },
  degraded: { label: 'Degraded', dot: 'bg-amber-400', text: 'text-amber-300', ring: 'ring-amber-500/30 bg-amber-500/10' },
  unknown: { label: 'Unknown', dot: 'bg-slate-500', text: 'text-slate-300', ring: 'ring-slate-500/30 bg-slate-500/10' },
  disabled: { label: 'Disabled', dot: 'bg-slate-600', text: 'text-slate-400', ring: 'ring-slate-600/30 bg-slate-600/10' },
};
