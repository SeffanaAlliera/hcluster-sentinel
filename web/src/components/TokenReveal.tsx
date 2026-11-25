'use client';
import { useEffect, useState } from 'react';

export function TokenReveal({ nodeId, token, onClose }: { nodeId: string; token: string; onClose?: () => void }) {
  const [origin, setOrigin] = useState('https://YOUR-APP.vercel.app');
  const [copied, setCopied] = useState<string | null>(null);
  useEffect(() => setOrigin(window.location.origin), []);
  const copy = async (k: string, v: string) => { await navigator.clipboard.writeText(v); setCopied(k); setTimeout(() => setCopied(null), 1500); };
  const Row = ({ k, label, value }: { k: string; label: string; value: string }) => (
    <div><div className="label">{label}</div>
      <div className="flex gap-2"><code className="input select-all overflow-x-auto whitespace-nowrap font-mono text-xs">{value}</code>
        <button type="button" className="btn shrink-0" onClick={() => copy(k, value)}>{copied === k ? 'Copied' : 'Copy'}</button></div></div>
  );
  return (
    <div className="card space-y-4 border-amber-500/40 p-5">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
        <strong>Copy the token now.</strong> It is shown only once and only its hash is stored. If you lose it, rotate the token.
      </div>
      <Row k="id" label="Node ID" value={nodeId} />
      <Row k="url" label="API URL" value={origin} />
      <Row k="tok" label="API token (secret)" value={token} />
      <div className="text-sm text-slate-300">On the node, run the installer and paste these three values when asked:
        <pre className="mt-2 overflow-x-auto rounded-lg bg-ink-950 p-3 font-mono text-xs text-slate-300">sudo ./agent/install.sh</pre></div>
      {onClose && <button className="btn" onClick={onClose}>I have saved the token</button>}
    </div>
  );
}
