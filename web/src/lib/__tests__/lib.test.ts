import { describe, expect, it } from 'vitest';
import { buildToken, hashSecret, newSecret, parseBearer, parseToken, verifySecret } from '../tokens';
import { agentPayload, nodeCreate, parseRange } from '../validation';
import { effectiveNodeStatus, effectiveServiceStatus } from '../status';
import { memoryLimit } from '../ratelimit';
import { fmtTime, tzLabel, zoneParts } from '../format';
import { pageCount, pageWindow, parsePage } from '../paging';
import { drives, nics, pick, seriesKey, worstDrive, type LatestRow } from '../metrics';

const NODE = '11111111-2222-3333-4444-555555555555';
const base = (over: object = {}) => ({ schema: 1, node_id: NODE, reports: [{ collected_at: '2026-01-01T00:00:00Z', ...over }] });

describe('tokens', () => {
  it('round-trips and verifies', () => {
    const secret = newSecret();
    const tok = buildToken(NODE, secret);
    const p = parseToken(tok)!;
    expect(p.credentialId).toBe(NODE);
    expect(verifySecret(p.secret, hashSecret(secret))).toBe(true);
    expect(verifySecret(newSecret(), hashSecret(secret))).toBe(false);
  });
  it('rejects malformed tokens and headers', () => {
    expect(parseToken('hcs_x_y')).toBeNull();
    expect(parseToken(null)).toBeNull();
    expect(parseBearer('Basic abc')).toBeNull();
    expect(parseBearer('Bearer abc')).toBe('abc');
  });
  it('secrets are unique and 43 chars', () => {
    expect(newSecret()).toHaveLength(43);
    expect(newSecret()).not.toBe(newSecret());
  });
  it('does not accept a stored-hash mismatch of different length', () => {
    expect(verifySecret(newSecret(), 'abcd')).toBe(false);
  });
});

describe('agent payload validation', () => {
  it('accepts a realistic report', () => {
    const r = agentPayload.safeParse(base({
      host: { hostname: 'h1', os: 'Ubuntu', arch: 'x86_64', kernel: '6', uptime_seconds: 5 },
      services: [{ key: 'jellyfin', name: 'Jellyfin', type: 'http', target: 'http://127.0.0.1:8096', status: 'operational', response_ms: 12, http_status: 200, error: null, checked_at: '2026-01-01T00:00:00Z' }],
      metrics_report: { status: 'ok', prometheus: { status: 'ok', error: null, targets_up: 3, targets_total: 3, down_targets: [] }, values: [{ name: 'cpu_percent', value: 1.5, source: 'prometheus' }], unavailable: [] },
    }));
    expect(r.success).toBe(true);
  });
  it('rejects bad enums, keys, metric names, NaN-ish and oversize arrays', () => {
    const svc = { key: 'x', name: 'x', type: 'http', status: 'operational' };
    expect(agentPayload.safeParse(base({ services: [{ ...svc, status: 'weird' }] })).success).toBe(false);
    expect(agentPayload.safeParse(base({ services: [{ ...svc, key: 'Bad Key!' }] })).success).toBe(false);
    expect(agentPayload.safeParse(base({ services: [{ ...svc, type: 'ftp' }] })).success).toBe(false);
    expect(agentPayload.safeParse(base({ services: Array(51).fill(svc) })).success).toBe(false);
    const mr = (v: unknown, name = 'cpu_percent') => ({ status: 'ok', prometheus: { status: 'ok' }, values: [{ name, value: v }] });
    expect(agentPayload.safeParse(base({ metrics_report: mr(1e30) })).success).toBe(false);
    expect(agentPayload.safeParse(base({ metrics_report: mr('1') })).success).toBe(false);
    expect(agentPayload.safeParse(base({ metrics_report: mr(1, "x'; drop table") })).success).toBe(false);
    expect(agentPayload.safeParse({ ...base(), reports: [] }).success).toBe(false);
    expect(agentPayload.safeParse({ ...base(), schema: 2 }).success).toBe(false);
    expect(agentPayload.safeParse({ ...base(), node_id: 'nope' }).success).toBe(false);
    expect(agentPayload.safeParse(base({ collected_at: 'yesterday' })).success).toBe(false);
  });
  it('sanitises control characters, truncates, and drops unknown fields', () => {
    const r = agentPayload.parse(base({ services: [{ key: 'a', name: 'A\u0000\u001bB', type: 'tcp', status: 'down', error: 'x'.repeat(2000).slice(0, 900), evil: '<script>' }] }));
    const s = r.reports[0].services![0] as Record<string, unknown>;
    expect(s.name).toBe('A  B');
    expect((s.error as string).length).toBe(500);
    expect('evil' in s).toBe(false);
  });
});

describe('node create / ranges / status', () => {
  it('validates nodes', () => {
    expect(nodeCreate.safeParse({ hostname: 'h2', display_name: 'Two' }).success).toBe(true);
    expect(nodeCreate.safeParse({ hostname: 'h 2', display_name: 'Two' }).success).toBe(false);
    expect(nodeCreate.safeParse({ hostname: 'h2', display_name: 'T', heartbeat_interval_seconds: 120, offline_threshold_seconds: 60 }).success).toBe(false);
  });
  it('parses ranges with a safe default', () => {
    expect(parseRange('7d')).toBe('7d');
    expect(parseRange('999y')).toBe('24h');
    expect(parseRange(null)).toBe('24h');
  });
  it('computes effective status from heartbeat age', () => {
    const now = Date.parse('2026-01-01T00:10:00Z');
    const n = { enabled: true, status: 'online' as const, offline_threshold_seconds: 180 };
    expect(effectiveNodeStatus({ ...n, last_heartbeat_at: '2026-01-01T00:09:00Z' }, now)).toBe('online');
    expect(effectiveNodeStatus({ ...n, last_heartbeat_at: '2026-01-01T00:05:00Z' }, now)).toBe('offline');
    expect(effectiveNodeStatus({ ...n, last_heartbeat_at: null }, now)).toBe('unknown');
    expect(effectiveNodeStatus({ ...n, enabled: false, last_heartbeat_at: '2026-01-01T00:09:59Z' }, now)).toBe('unknown');
    expect(effectiveNodeStatus({ ...n, status: 'degraded', last_heartbeat_at: '2026-01-01T00:09:59Z' }, now)).toBe('degraded');
  });
  it('services are unknown when their node is silent', () => {
    expect(effectiveServiceStatus({ status: 'operational' }, 'offline')).toBe('unknown');
    expect(effectiveServiceStatus({ status: 'down' }, 'degraded')).toBe('down');
  });
});

describe('memory limiter', () => {
  it('limits per window', () => {
    const k = 'k' + Math.random();
    expect([1, 2, 3, 4].map((i) => memoryLimit(k, 3, 1000, 1000 + i))).toEqual([true, true, true, false]);
    expect(memoryLimit(k, 3, 1000, 5000)).toBe(true);
  });
});

describe('labeled metrics', () => {
  const row = (name: string, label: string, value: number): LatestRow => ({ name, label, value, source: 'local', ts: 'x' });
  const rows = [row('disk_percent', '/mnt/media', 85), row('disk_percent', '/', 40), row('disk_available_bytes', '/', 5e9), row('disk_total_bytes', '/', 1e10),
    row('cpu_temp_celsius', '', 55), row('net_rx_bytes_per_sec', '', 300), row('net_rx_bytes_per_sec', 'enp1s0', 100), row('net_tx_bytes_per_sec', 'enp2s0', 5)];
  it('lists every drive, root first, and finds the fullest', () => {
    expect(drives(rows).map((d) => d.mount)).toEqual(['/', '/mnt/media']);
    expect(drives(rows)[0]).toMatchObject({ percent: 40, available: 5e9, total: 1e10 });
    expect(worstDrive(rows)?.mount).toBe('/mnt/media');
    expect(worstDrive([])).toBeNull();
  });
  it('separates node-wide from labeled values', () => {
    expect(pick(rows, 'cpu_temp_celsius')).toBe(55);
    expect(pick(rows, 'disk_percent')).toBeUndefined();          // no unlabeled disk value: never invented
    expect(pick(rows, 'net_rx_bytes_per_sec')).toBe(300);
    expect(nics(rows)).toEqual(['enp1s0', 'enp2s0']);
    expect(seriesKey('disk_percent', '/mnt/x')).toBe('disk_percent|/mnt/x');
    expect(seriesKey('cpu_percent', '')).toBe('cpu_percent');
  });
  it('validates labels and the dns service type', () => {
    const mr = (v: object) => base({ metrics_report: { status: 'ok', prometheus: { status: 'ok' }, values: [v] } });
    const p = agentPayload.parse(mr({ name: 'disk_percent', label: '/mnt/a\u0000b', value: 3 }));
    expect(p.reports[0].metrics_report!.values[0].label).toBe('/mnt/a b');
    expect(agentPayload.parse(mr({ name: 'cpu_percent', value: 3 })).reports[0].metrics_report!.values[0].label).toBe('');
    expect(agentPayload.parse(mr({ name: 'disk_percent', label: 'x'.repeat(100), value: 3 })).reports[0].metrics_report!.values[0].label).toHaveLength(64);
    expect(agentPayload.safeParse(base({ services: [{ key: 'dnsmasq', name: 'dnsmasq', type: 'dns', status: 'operational' }] })).success).toBe(true);
  });
});

describe('node-local time display', () => {
  it('shows the instant in the node timezone, with an explicit offset', () => {
    expect(fmtTime('2026-11-26T07:10:32Z', 'Asia/Manila')).toBe('2026-11-26 15:10:32 UTC+8');
    expect(fmtTime('2026-11-26T07:10:32Z', 'America/New_York')).toBe('2026-11-26 02:10:32 UTC-5');
    expect(fmtTime('2026-11-26T07:10:32Z', null)).toBe('2026-11-26 07:10:32 UTC');
    expect(fmtTime(null, 'Asia/Manila')).toBe('—');
  });
  it('falls back to UTC for an unknown zone instead of crashing', () => {
    expect(fmtTime('2026-11-26T07:10:32Z', 'Not/AZone')).toBe('2026-11-26 07:10:32 UTC');
  });
  it('chart axis parts and zone label', () => {
    const z = zoneParts(Date.parse('2026-11-26T23:30:00Z'), 'Asia/Manila');
    expect([z.D, z.h, z.m]).toEqual(['27', '07', '30']);                 // crosses midnight in node time
    expect(tzLabel('Asia/Manila')).toBe('Asia/Manila (UTC+8)');
    expect(tzLabel(null)).toBe('UTC');
  });
  it('accepts the agent-reported timezone', () => {
    expect(agentPayload.parse(base({ host: { timezone: 'Asia/Manila' } })).reports[0].host!.timezone).toBe('Asia/Manila');
  });
});

describe('incident paging', () => {
  it('parsePage only accepts positive integers', () => {
    expect([undefined, '', 'abc', '0', '-3', '2.5', '1e3', '999999'].map(parsePage)).toEqual([1, 1, 1, 1, 1, 1, 1, 1]);
    expect([parsePage('1'), parsePage('7'), parsePage(['3', '9'])]).toEqual([1, 7, 3]);
  });
  it('pageCount is at least 1 and rounds up', () => {
    expect([pageCount(0, 15), pageCount(15, 15), pageCount(16, 15), pageCount(100, 15)]).toEqual([1, 1, 2, 7]);
  });
  it('pageWindow shows first, last and a window with gaps', () => {
    expect(pageWindow(1, 1)).toEqual([1]);
    expect(pageWindow(1, 3)).toEqual([1, 2, 3]);
    expect(pageWindow(10, 20)).toEqual([1, null, 8, 9, 10, 11, 12, null, 20]);
    expect(pageWindow(2, 20)).toEqual([1, 2, 3, 4, null, 20]);
  });
});
