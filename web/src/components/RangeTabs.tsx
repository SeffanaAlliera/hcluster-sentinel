import Link from 'next/link';
import { RANGES, type RangeKey } from '@/lib/validation';
const LABEL: Record<RangeKey, string> = { '1h': 'Last hour', '6h': '6 hours', '24h': '24 hours', '7d': '7 days' };
export function RangeTabs({ base, current }: { base: string; current: RangeKey }) {
  return (
    <div className="inline-flex rounded-lg border border-ink-600 bg-ink-900 p-0.5" role="tablist" aria-label="Time range">
      {(Object.keys(RANGES) as RangeKey[]).map((r) => (
        <Link key={r} role="tab" aria-selected={r === current} href={`${base}?range=${r}`} scroll={false}
          className={`rounded-md px-3 py-1 text-xs font-medium ${r === current ? 'bg-sky-600 text-white' : 'text-slate-300 hover:bg-ink-700'}`}>{LABEL[r]}</Link>
      ))}
    </div>
  );
}
