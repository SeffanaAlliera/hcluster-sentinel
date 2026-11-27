import Link from 'next/link';
import { pageWindow } from '@/lib/paging';

/** Server-rendered pager. `href(p)` builds the URL for page p. */
export function Pager({ page, last, total, size, href }: { page: number; last: number; total: number; size: number; href: (p: number) => string }) {
  const from = total === 0 ? 0 : (page - 1) * size + 1, to = Math.min(total, page * size);
  const cell = 'rounded-md px-2.5 py-1 text-xs font-medium';
  return (
    <nav className="flex flex-wrap items-center justify-between gap-2 border-t border-ink-700 px-4 py-3 text-xs text-slate-400" aria-label="Pagination">
      <span>Showing {from}–{to} of {total}</span>
      <div className="flex items-center gap-1">
        {page > 1 ? <Link href={href(page - 1)} className={`${cell} text-slate-300 hover:bg-ink-700`}>← Prev</Link> : <span className={`${cell} text-slate-600`}>← Prev</span>}
        {pageWindow(page, last).map((p, i) => p === null
          ? <span key={`g${i}`} className="px-1 text-slate-600">…</span>
          : <Link key={p} href={href(p)} aria-current={p === page ? 'page' : undefined}
              className={`${cell} ${p === page ? 'bg-sky-600 text-white' : 'text-slate-300 hover:bg-ink-700'}`}>{p}</Link>)}
        {page < last ? <Link href={href(page + 1)} className={`${cell} text-slate-300 hover:bg-ink-700`}>Next →</Link> : <span className={`${cell} text-slate-600`}>Next →</span>}
      </div>
    </nav>
  );
}
