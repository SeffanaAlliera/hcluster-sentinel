'use client';
import { zoneParts } from '@/lib/format';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

export interface ChartSeries { key: string; label: string; color: string }
type Point = Record<string, number | null> & { t: number };

const fmtBytes = (n: number) => { const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let i = 0, v = n; while (v >= 1024 && i < 3) { v /= 1024; i++; } return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`; };

export function MetricChart({ title, subtitle, data, series, unit, spanMs, tz, wide }: { title: string; subtitle?: string; data: Point[]; series: ChartSeries[]; unit: 'percent' | 'bytes' | 'celsius' | 'mhz'; spanMs: number; tz?: string | null; wide?: boolean }) {
  const tick = (t: number) => { const z = zoneParts(t, tz); return spanMs > 86_400_000 * 1.5 ? `${Number(z.M)}/${Number(z.D)} ${z.h}h` : `${z.h}:${z.m}`; };   // node-local time
  const zoneName = zoneParts(Date.now(), tz).zone;
  const fmt = (v: number) => (unit === 'percent' ? `${v.toFixed(1)}%` : unit === 'celsius' ? `${v.toFixed(1)}°C` : unit === 'mhz' ? `${v.toFixed(0)} MHz` : fmtBytes(v));
  const axis = (v: number) => (unit === 'percent' ? `${v}%` : unit === 'celsius' ? `${v}°` : unit === 'mhz' ? `${v}` : fmtBytes(v));
  return (
    <div className={`card p-4${wide ? ' lg:col-span-2' : ''}`}>
      <div className="mb-2 flex items-start justify-between gap-3"><div className="min-w-0"><h3 className="text-sm font-medium text-slate-200">{title}</h3>{subtitle && <p className="truncate text-xs text-slate-400" title={subtitle}>{subtitle}</p>}</div><span className="shrink-0 text-[11px] text-slate-500" title={tz ? `Node timezone: ${tz}` : 'Node timezone unknown, showing UTC'}>{zoneName}</span></div>
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
              <Tooltip contentStyle={{ background: '#0b1220', border: '1px solid #2a3a58', borderRadius: 8, fontSize: 12 }} labelFormatter={(t) => { const z = zoneParts(Number(t), tz); return `${z.Y}-${z.M}-${z.D} ${z.h}:${z.m}:${z.s} ${z.zone}`; }} formatter={(v, name) => [fmt(Number(v)), name]} />
              {series.map((s) => (<Area key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color} fill={`url(#g-${s.key})`} strokeWidth={1.8} dot={false} connectNulls={false} isAnimationActive={false} />))}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      {series.length > 1 && <div className="mt-2 flex max-h-24 flex-wrap gap-x-4 gap-y-1 overflow-y-auto text-xs text-slate-400">{series.map((s) => (<span key={s.key} className="inline-flex max-w-full items-center gap-1.5"><span className="h-2 w-2 shrink-0 rounded-full" style={{ background: s.color }} /><span className="truncate">{s.label}</span></span>))}</div>}
    </div>
  );
}
