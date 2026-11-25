'use client';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { TokenReveal } from './TokenReveal';

export function NodeActions({ id, enabled, hb, off }: { id: string; enabled: boolean; hb: number; off: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [edit, setEdit] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function req(url: string, method: string, body?: object) {
    setBusy(true); setErr(null);
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    setBusy(false);
    if (!res.ok) { setErr(data.issues?.join('; ') ?? data.error ?? 'Request failed'); return null; }
    return data;
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <button className="btn" disabled={busy} onClick={async () => { if (await req(`/api/nodes/${id}`, 'PATCH', { enabled: !enabled })) router.refresh(); }}>{enabled ? 'Disable' : 'Enable'}</button>
        <button className="btn" disabled={busy} onClick={() => setEdit((v) => !v)}>Thresholds</button>
        <button className="btn" disabled={busy} onClick={async () => { if (confirm('Rotate the token? The agent stops working until you update it with the new one.')) { const d = await req(`/api/nodes/${id}/rotate-token`, 'POST'); if (d) setToken(d.token); } }}>Rotate token</button>
        <button className="btn btn-danger" disabled={busy} onClick={async () => { if (confirm('Delete this node and ALL its history? This cannot be undone.') && (await req(`/api/nodes/${id}`, 'DELETE'))) { router.push('/'); router.refresh(); } }}>Remove node</button>
      </div>
      {edit && (
        <form className="card flex flex-wrap items-end gap-3 p-3" onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); if (await req(`/api/nodes/${id}`, 'PATCH', { heartbeat_interval_seconds: Number(f.get('hb')), offline_threshold_seconds: Number(f.get('off')) })) { setEdit(false); router.refresh(); } }}>
          <div><label className="label">Heartbeat (s)</label><input name="hb" type="number" defaultValue={hb} min={5} className="input w-28" /></div>
          <div><label className="label">Offline after (s)</label><input name="off" type="number" defaultValue={off} min={10} className="input w-28" /></div>
          <button className="btn btn-primary" disabled={busy}>Save</button>
        </form>
      )}
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      {token && <TokenReveal nodeId={id} token={token} onClose={() => setToken(null)} />}
    </div>
  );
}
