// Test double for the Supabase service-role client, backed by a real in-process Postgres (PGlite)
// running the project's actual migrations. Lets us exercise the real route handlers + real SQL.
import { PGlite } from '@electric-sql/pglite';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

const CASTS: Record<string, string> = { p_reports: 'jsonb', p_node_id: 'uuid', p_credential_id: 'uuid' };

export async function makeDb() {
  const db = new PGlite();
  await db.exec(`
    create role anon nologin; create role authenticated nologin; create role service_role nologin;
    create schema auth; create table auth.users (id uuid primary key default gen_random_uuid(), email text);
    create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
    grant usage on schema public, auth to anon, authenticated, service_role;`);
  const dir = path.resolve(__dirname, '../../../supabase/migrations');
  for (const f of readdirSync(dir).sort().filter((f) => !f.includes('cron'))) await db.exec(readFileSync(path.join(dir, f), 'utf8'));

  const client = {
    async rpc(fn: string, args: Record<string, unknown> = {}) {
      const keys = Object.keys(args);
      const call = `public.${fn}(${keys.map((k, i) => `${k} => $${i + 1}${CASTS[k] ? '::' + CASTS[k] : ''}`).join(', ')})`;
      const vals = keys.map((k) => (typeof args[k] === 'object' ? JSON.stringify(args[k]) : args[k]));
      try {
        if (fn === 'register_node') return { data: (await db.query(`select * from ${call}`, vals)).rows, error: null };
        return { data: ((await db.query(`select ${call} as r`, vals)).rows[0] as { r: unknown }).r, error: null };
      } catch (e) { return { data: null, error: { message: (e as Error).message } }; }
    },
    from(table: string) {
      const where: [string, unknown][] = [];
      const chain = {
        select: () => chain,
        eq: (c: string, v: unknown) => { where.push([c, v]); return chain; },
        async maybeSingle() {
          const sql = `select * from public.${table}` + (where.length ? ' where ' + where.map(([c], i) => `${c} = $${i + 1}`).join(' and ') : '');
          return { data: (await db.query(sql, where.map(([, v]) => v))).rows[0] ?? null, error: null };
        },
      };
      return chain;
    },
  };
  return { db, client };
}
