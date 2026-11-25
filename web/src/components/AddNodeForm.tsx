'use client';
import Link from 'next/link';
import { useState } from 'react';
import { TokenReveal } from './TokenReveal';

export function AddNodeForm() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<{ node_id: string; token: string } | null>(null);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault(); setBusy(true); setErr(null);
    const f = new FormData(e.currentTarget);
    const res = await fetch('/api/nodes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hostname: f.get('hostname'), display_name: f.get('display_name'), heartbeat_interval_seconds: Number(f.get('hb')), offline_threshold_seconds: Number(f.get('off')) }),
    });
    const body = await res.json().catch(() => ({}));
    setBusy(false);
    if (!res.ok) return setErr(body.issues?.join('; ') ?? body.error ?? 'Failed to create node');
    setDone(body);
  }

  if (done) return (<div className="space-y-4"><TokenReveal nodeId={done.node_id} token={done.token} /><Link className="btn btn-primary" href={`/nodes/${done.node_id}`}>Go to node</Link></div>);
  return (
    <form onSubmit={submit} className="card max-w-xl space-y-4 p-5">
      <div><label className="label" htmlFor="hostname">Hostname</label><input id="hostname" name="hostname" required placeholder="h2" className="input" pattern="[A-Za-z0-9._-]+" /></div>
      <div><label className="label" htmlFor="display_name">Display name</label><input id="display_name" name="display_name" required placeholder="Home server 2" className="input" maxLength={80} /></div>
      <div className="grid grid-cols-2 gap-4">
        <div><label className="label" htmlFor="hb">Heartbeat interval (s)</label><input id="hb" name="hb" type="number" min={5} max={3600} defaultValue={60} className="input" /></div>
        <div><label className="label" htmlFor="off">Offline threshold (s)</label><input id="off" name="off" type="number" min={10} max={86400} defaultValue={180} className="input" /></div>
      </div>
      {err && <p role="alert" className="text-sm text-rose-300">{err}</p>}
      <button className="btn btn-primary" disabled={busy}>{busy ? 'Creating…' : 'Create node & generate token'}</button>
    </form>
  );
}
