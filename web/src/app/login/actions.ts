'use server';
import { redirect } from 'next/navigation';
import { userClient } from '@/lib/supabase/server';
import { memoryLimit } from '@/lib/ratelimit';
import { headers } from 'next/headers';

export async function signIn(_: { error?: string } | undefined, form: FormData) {
  const h = await headers();
  const ip = h.get('x-real-ip') ?? h.get('x-forwarded-for') ?? 'unknown';
  if (!memoryLimit(`login:${ip}`, 10, 10 * 60_000)) return { error: 'Too many attempts. Try again later.' };
  const email = String(form.get('email') ?? '').trim();
  const password = String(form.get('password') ?? '');
  if (!email || !password) return { error: 'Enter your email and password.' };
  const sb = await userClient();
  const { data, error } = await sb.auth.signInWithPassword({ email, password });
  if (error || !data.user) return { error: 'Invalid email or password.' };
  const { data: admin } = await sb.from('admin_users').select('user_id').eq('user_id', data.user.id).maybeSingle();
  if (!admin) { await sb.auth.signOut(); return { error: 'This account is not authorised for this dashboard.' }; }
  redirect('/');
}

export async function signOut() {
  const sb = await userClient();
  await sb.auth.signOut();
  redirect('/login');
}
