import { STATUS_STYLE } from '@/lib/status';
export function StatusBadge({ status, label, small }: { status: string; label?: string; small?: boolean }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.unknown;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 ${small ? 'py-0 text-[11px]' : 'py-0.5 text-xs'} font-medium ring-1 ring-inset ${s.ring} ${s.text}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden />{label ?? s.label}
    </span>
  );
}
