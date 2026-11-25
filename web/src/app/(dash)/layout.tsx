import Link from 'next/link';
import { requireAdminPage } from '@/lib/auth';
import { signOut } from '@/app/login/actions';
import { AutoRefresh } from '@/components/AutoRefresh';

export const dynamic = 'force-dynamic';

export default async function DashLayout({ children }: { children: React.ReactNode }) {
  const { user } = await requireAdminPage();
  const nav = [['/', 'Overview'], ['/services', 'Services'], ['/incidents', 'Incidents']] as const;
  return (
    <>
      <header className="sticky top-0 z-10 border-b border-ink-700 bg-ink-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
          <Link href="/" className="text-base font-semibold tracking-tight text-white">hCluster <span className="text-sky-400">Sentinel</span></Link>
          <nav className="flex gap-1 text-sm">{nav.map(([href, label]) => (<Link key={href} href={href} className="rounded-md px-3 py-1.5 text-slate-300 hover:bg-ink-800 hover:text-white">{label}</Link>))}</nav>
          <div className="ml-auto flex items-center gap-3 text-xs text-slate-400">
            <span className="hidden sm:inline">{user.email}</span>
            <form action={signOut}><button className="btn !py-1">Sign out</button></form>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      <AutoRefresh seconds={30} />
    </>
  );
}
