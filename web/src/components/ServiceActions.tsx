'use client';
import { useRouter } from 'next/navigation';
import { useState } from 'react';

export function ServiceActions({ id, enabled }: { id: string; enabled: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  async function call(method: 'PATCH' | 'DELETE', body?: object) {
    setBusy(true);
    await fetch(`/api/services/${id}`, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    setBusy(false); router.refresh();
  }
  return (
    <span className="inline-flex gap-1">
      <button className="btn !px-2 !py-0.5 text-xs" disabled={busy} onClick={() => call('PATCH', { enabled: !enabled })}>{enabled ? 'Mute' : 'Unmute'}</button>
      <button className="btn btn-danger !px-2 !py-0.5 text-xs" disabled={busy} onClick={() => confirm('Remove this service record? It returns if the agent still reports it.') && call('DELETE')}>Remove</button>
    </span>
  );
}
