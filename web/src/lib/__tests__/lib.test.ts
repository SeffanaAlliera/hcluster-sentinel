import { describe, expect, it } from 'vitest';
import { buildToken, hashSecret, newSecret, parseBearer, parseToken, verifySecret } from '../tokens';
import { agentPayload, nodeCreate, parseRange } from '../validation';
import { effectiveNodeStatus, effectiveServiceStatus } from '../status';
import { memoryLimit } from '../ratelimit';

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
