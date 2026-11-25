export function StatCard({ label, value, tone }: { label: string; value: number | string; tone?: 'good' | 'bad' | 'warn' }) {
  const c = tone === 'good' ? 'text-emerald-300' : tone === 'bad' ? 'text-rose-300' : tone === 'warn' ? 'text-amber-300' : 'text-white';
  return (<div className="card px-4 py-3"><div className="text-xs uppercase tracking-wide text-slate-400">{label}</div><div className={`mt-1 text-2xl font-semibold tabular-nums ${c}`}>{value}</div></div>);
}
