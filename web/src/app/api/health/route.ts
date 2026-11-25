import { NextResponse } from 'next/server';
export const dynamic = 'force-dynamic';
// Deliberately reveals nothing about the deployment.
export const GET = () => NextResponse.json({ ok: true });
