export const INCIDENT_PAGE_SIZE = 15;
export const NODE_PREVIEW = 5;

/** Untrusted ?page= value -> integer >= 1 (anything else is page 1). */
export function parsePage(raw: string | string[] | undefined): number {
  const v = Array.isArray(raw) ? raw[0] : raw;
  if (typeof v !== 'string' || !/^\d{1,6}$/.test(v)) return 1;
  const n = Number(v);
  return n >= 1 && n <= 100000 ? n : 1;
}

export function pageCount(total: number, size: number): number {
  return Math.max(1, Math.ceil(total / size));
}

/** Page numbers to render: first, last, and a window around the current page; null = gap ("…"). */
export function pageWindow(current: number, last: number, span = 2): (number | null)[] {
  const keep = new Set<number>([1, last]);
  for (let p = current - span; p <= current + span; p++) if (p >= 1 && p <= last) keep.add(p);
  const sorted = [...keep].sort((a, b) => a - b);
  const out: (number | null)[] = [];
  sorted.forEach((p, i) => { if (i > 0 && p - sorted[i - 1] > 1) out.push(null); out.push(p); });
  return out;
}
