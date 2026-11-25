'use client';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

export interface ChartSeries { key: string; label: string; color: string }
type Point = Record<string, number | null> & { t: number };

const fmtBytes = (n: number) => { const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let i = 0, v = n; while (v >= 1024 && i < 3) { v /= 1024; i++; } return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`; };

export function MetricChart({ title, data, series, unit, spanMs }: { title: string; data: Point[]; series: ChartSeries[]; unit: 'percent' | 'bytes' | 'celsius'; spanMs: number }) {
  const tick = (t: number) => { const d = new Date(t); return spanMs > 86_400_000 * 1.5 ? `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${String(d.getUTCHours()).padStart(2, '0')}h` : `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`; };
  const fmt = (v: number) => (unit === 'percent' ? `${v.toFixed(1)}%` : unit === 'celsius' ? `${v.toFixed(1)}°C` : fmtBytes(v));
  const axis = (v: number) => (unit === 'percent' ? `${v}%` : unit === 'celsius' ? `${v}°` : fmtBytes(v));
  return (
    <div className="card p-4">
      <div className="mb-2 flex items-center justify-between"><h3 className="text-sm font-medium text-slate-200">{title}</h3><span className="text-[11px] text-slate-500">UTC</span></div>
      {data.length === 0 ? (
        <div className="flex h-48 items-center justify-center text-sm text-slate-500">No data for this range</div>
      ) : (
        <div className="h-48" role="img" aria-label={`${title} over time`}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <defs>{series.map((s) => (<linearGradient key={s.key} id={`g-${s.key}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={s.color} stopOpacity={0.35} /><stop offset="100%" stopColor={s.color} stopOpacity={0} /></linearGradient>))}</defs>
              <CartesianGrid stroke="#1c2840" vertical={false} />
              <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick} tick={{ fill: '#8b9ab5', fontSize: 11 }} stroke="#2a3a58" minTickGap={40} />
              <YAxis tickFormatter={axis} domain={unit === 'percent' ? [0, 100] : [0, 'auto']} width={unit === 'bytes' ? 68 : 44} tick={{ fill: '#8b9ab5', fontSize: 11 }} stroke="#2a3a58" />
              <Tooltip contentStyle={{ background: '#0b1220', border: '1px solid #2a3a58', borderRadius: 8, fontSize: 12 }} labelFormatter={(t) => new Date(Number(t)).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'} formatter={(v, name) => [fmt(Number(v)), name]} />
              {series.map((s) => (<Area key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color} fill={`url(#g-${s.key})`} strokeWidth={1.8} dot={false} connectNulls={false} isAnimationActive={false} />))}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      {series.length > 1 && <div className="mt-2 flex gap-4 text-xs text-slate-400">{series.map((s) => (<span key={s.key} className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: s.color }} />{s.label}</span>))}</div>}
    </div>
  );
}
