import { createHash } from 'node:crypto';

/** Per-instance sliding window. Cheap first line of defence; the DB limiter is the shared one. */
const buckets = new Map<string, number[]>();
export function memoryLimit(key: string, limit: number, windowMs: number, now = Date.now()): boolean {
  const arr = (buckets.get(key) ?? []).filter((t) => now - t < windowMs);
  if (arr.length >= limit) { buckets.set(key, arr); return false; }
  arr.push(now);
  buckets.set(key, arr);
  if (buckets.size > 5000) for (const k of buckets.keys()) { if (!(buckets.get(k)?.some((t) => now - t < windowMs))) buckets.delete(k); }
  return true;
}
export function clientIp(headers: Headers): string {
  return (headers.get('x-real-ip') ?? headers.get('x-forwarded-for')?.split(',')[0] ?? 'unknown').trim();
}
export const ipKey = (ip: string) => createHash('sha256').update(ip).digest('hex').slice(0, 16);
