export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never';
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 5) return 'just now';
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
export function bytes(n: number | null | undefined): string {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
export const pct = (n: number | null | undefined) => (n == null ? '—' : `${n.toFixed(n >= 10 ? 0 : 1)}%`);
/** Wall-clock parts of an instant in an IANA timezone (the NODE's own zone). Unknown/missing zone => UTC. */
export function zoneParts(ms: number, tz?: string | null) {
  const mk = (zone: string) => new Intl.DateTimeFormat('en-GB', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'shortOffset' }).formatToParts(new Date(ms));
  let parts; try { parts = mk(tz || 'UTC'); } catch { parts = mk('UTC'); }
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  const off = g('timeZoneName').replace('GMT', 'UTC');
  return { Y: g('year'), M: g('month'), D: g('day'), h: g('hour'), m: g('minute'), s: g('second'), zone: off === 'UTC' || off === 'UTC+0' ? 'UTC' : off };
}
/** "2026-11-26 15:10:32 UTC+8": the time as the node's own clock/timezone shows it. */
export function fmtTime(iso: string | null | undefined, tz?: string | null): string {
  if (!iso) return '—';
  const z = zoneParts(Date.parse(iso), tz);
  return `${z.Y}-${z.M}-${z.D} ${z.h}:${z.m}:${z.s} ${z.zone}`;
}
/** Short label for a timezone, e.g. "Asia/Manila (UTC+8)"; plain "UTC" when unknown. */
export const tzLabel = (tz?: string | null, nowMs = Date.now()) => (tz ? `${tz} (${zoneParts(nowMs, tz).zone})` : 'UTC');
export const utc = (iso: string | null | undefined) => (iso ? new Date(iso).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : '—');
export function incidentLength(start: string, end: string | null, now = Date.now()): string {
  return duration(Math.round(((end ? Date.parse(end) : now) - Date.parse(start)) / 1000));
}
