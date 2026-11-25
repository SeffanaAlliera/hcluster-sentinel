// Pure helpers for labeled metrics (label '' = node-wide; otherwise a mountpoint or interface name).
export interface LatestRow { name: string; label: string; value: number; source: string; ts: string }
export interface Drive { mount: string; percent: number | null; available: number | null; total: number | null }

export const pick = (rows: LatestRow[], name: string, label = ''): number | undefined => rows.find((r) => r.name === name && r.label === label)?.value;

export function drives(rows: LatestRow[]): Drive[] {
  const mounts = [...new Set(rows.filter((r) => r.name === 'disk_percent' || r.name === 'disk_total_bytes').map((r) => r.label).filter(Boolean))];
  return mounts.map((m) => ({ mount: m, percent: pick(rows, 'disk_percent', m) ?? null, available: pick(rows, 'disk_available_bytes', m) ?? null, total: pick(rows, 'disk_total_bytes', m) ?? null }))
    .sort((a, b) => (a.mount === '/' ? -1 : b.mount === '/' ? 1 : a.mount.localeCompare(b.mount)));
}
/** The drive that matters most for a summary: the fullest one. */
export function worstDrive(rows: LatestRow[]): Drive | null {
  return drives(rows).filter((d) => d.percent != null).sort((a, b) => (b.percent ?? 0) - (a.percent ?? 0))[0] ?? null;
}
export const nics = (rows: LatestRow[]): string[] =>
  [...new Set(rows.filter((r) => (r.name === 'net_rx_bytes_per_sec' || r.name === 'net_tx_bytes_per_sec') && r.label).map((r) => r.label))].sort();

/** Series key used by metricSeries: node-wide = name, labeled = `${name}|${label}`. */
export const seriesKey = (name: string, label: string) => (label ? `${name}|${label}` : name);
