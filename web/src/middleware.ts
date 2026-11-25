import { NextResponse, type NextRequest } from 'next/server';
import { updateSession } from '@/lib/supabase/middleware';

// Public paths: agent ingest (token-authenticated), cron (secret-authenticated), health, login.
const PUBLIC = [/^\/login/, /^\/api\/agent\//, /^\/api\/cron\//, /^\/api\/health$/];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (PUBLIC.some((re) => re.test(pathname))) return NextResponse.next();
  const { response, user, misconfigured } = await updateSession(request);
  if (misconfigured) return new NextResponse('Server not configured', { status: 500 });
  if (!user) {
    if (pathname.startsWith('/api/')) return NextResponse.json({ error: 'unauthorized' }, { status: 401 });
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.search = '';
    return NextResponse.redirect(url);
  }
  return response;
}

export const config = { matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'] };
