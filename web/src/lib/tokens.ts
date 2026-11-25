import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

const TOKEN_RE = /^hcs_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_([A-Za-z0-9_-]{43})$/;

export function newSecret(): string {
  return randomBytes(32).toString('base64url'); // 256 bits, 43 chars
}
export function hashSecret(secret: string): string {
  return createHash('sha256').update(secret).digest('hex');
}
/** token = hcs_<credentialId>_<secret>. Only the hash of <secret> is stored. */
export function buildToken(credentialId: string, secret: string): string {
  return `hcs_${credentialId}_${secret}`;
}
export function parseToken(token: string | null | undefined): { credentialId: string; secret: string } | null {
  if (!token) return null;
  const m = TOKEN_RE.exec(token.trim());
  return m ? { credentialId: m[1], secret: m[2] } : null;
}
export function parseBearer(header: string | null): string | null {
  const m = /^Bearer\s+(\S+)$/i.exec(header ?? '');
  return m ? m[1] : null;
}
export function verifySecret(secret: string, storedHash: string): boolean {
  const a = Buffer.from(hashSecret(secret), 'hex');
  const b = Buffer.from(storedHash, 'hex');
  return a.length === b.length && timingSafeEqual(a, b);
}
