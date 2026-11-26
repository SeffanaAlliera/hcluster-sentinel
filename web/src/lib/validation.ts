import { z } from 'zod';

// strip control characters, collapse nothing else; React escapes on output as well
// eslint-disable-next-line no-control-regex
const clean = (s: string) => s.replace(/[\u0000-\u001f\u007f]/g, ' ').trim();
const text = (max: number) => z.string().max(max * 2).transform((s) => clean(s).slice(0, max));
const optText = (max: number) => text(max).nullish();
const num = z.number().finite();

export const KEY_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
export const METRIC_RE = /^[a-z][a-z0-9_]{0,63}$/;
const iso = z.string().datetime({ offset: true });

const service = z.object({
  key: z.string().regex(KEY_RE),
  name: text(80).pipe(z.string().min(1)),
  type: z.enum(['http', 'tcp', 'dns', 'process']),
  target: optText(300),
  status: z.enum(['operational', 'degraded', 'down']),
  response_ms: z.number().int().min(0).max(600_000).nullish(),
  http_status: z.number().int().min(100).max(599).nullish(),
  error: optText(500),
  checked_at: iso.optional(),
});

const metricValue = z.object({
  name: z.string().regex(METRIC_RE),
  label: text(64).default(''),
  value: num.refine((v) => Math.abs(v) < 1e18, 'out of range'),
  source: z.enum(['prometheus', 'local']).default('prometheus'),
});

const metricsReport = z.object({
  status: z.enum(['ok', 'partial', 'failed', 'disabled']),
  prometheus: z.object({
    status: z.enum(['ok', 'unreachable', 'disabled']),
    error: optText(300),
    targets_up: z.number().int().min(0).max(100000).nullish(),
    targets_total: z.number().int().min(0).max(100000).nullish(),
    down_targets: z.array(text(120)).max(10).optional(),
  }),
  values: z.array(metricValue).max(128),
  unavailable: z.array(z.string().regex(METRIC_RE)).max(64).default([]),
});

export const report = z.object({
  collected_at: iso,
  agent: z.object({ version: text(32) }).partial().optional(),
  host: z.object({
    hostname: optText(253), os: optText(120), arch: optText(40), kernel: optText(80), cpu_model: optText(120), timezone: optText(64),
    cpu_cores: z.number().int().min(1).max(4096).nullish(),
    uptime_seconds: z.number().int().min(0).max(1e10).nullish(),
  }).partial().optional(),
  services: z.array(service).max(50).optional(),
  metrics_report: metricsReport.optional(),
});

export const agentPayload = z.object({
  schema: z.literal(1),
  node_id: z.string().uuid(),
  reports: z.array(report).min(1).max(20),
});
export type AgentPayload = z.infer<typeof agentPayload>;

const hostname = z.string().trim().min(1).max(253).regex(/^[A-Za-z0-9._-]+$/, 'letters, digits, . _ - only');
export const nodeCreate = z.object({
  hostname,
  display_name: z.string().trim().min(1).max(80),
  heartbeat_interval_seconds: z.number().int().min(5).max(3600).default(60),
  offline_threshold_seconds: z.number().int().min(10).max(86400).default(180),
}).refine((v) => v.offline_threshold_seconds > v.heartbeat_interval_seconds, {
  message: 'offline threshold must be greater than the heartbeat interval', path: ['offline_threshold_seconds'],
});
export const nodePatch = z.object({
  hostname: hostname.optional(),
  display_name: z.string().trim().min(1).max(80).optional(),
  enabled: z.boolean().optional(),
  heartbeat_interval_seconds: z.number().int().min(5).max(3600).optional(),
  offline_threshold_seconds: z.number().int().min(10).max(86400).optional(),
}).strict();
export const servicePatch = z.object({ enabled: z.boolean() }).strict();

export const RANGES = {
  '1h': { seconds: 3600, bucket: 60 },
  '6h': { seconds: 6 * 3600, bucket: 300 },
  '24h': { seconds: 86400, bucket: 900 },
  '7d': { seconds: 7 * 86400, bucket: 3600 },
} as const;
export type RangeKey = keyof typeof RANGES;
export const parseRange = (v: string | null | undefined): RangeKey => (v && v in RANGES ? (v as RangeKey) : '24h');
