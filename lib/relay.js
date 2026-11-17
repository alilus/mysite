import { timingSafeEqual } from 'node:crypto';

export const TTL_SECONDS = 60 * 60;

export function sendError(res, status, message) {
  return res.status(status).json({ error: message });
}

function constantTimeEqual(a, b) {
  const left = Buffer.from(String(a || ''), 'utf8');
  const right = Buffer.from(String(b || ''), 'utf8');
  return left.length === right.length && left.length > 0 && timingSafeEqual(left, right);
}

export function authorize(req, secretName) {
  const expected = process.env[secretName];
  if (!expected) return { ok: false, status: 503, message: `Missing server configuration: ${secretName}` };
  const header = req.headers.authorization || '';
  const actual = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!constantTimeEqual(actual, expected)) return { ok: false, status: 401, message: 'Unauthorized' };
  return { ok: true };
}

export async function redis(...command) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) throw new Error('Relay storage is not configured (Upstash Redis REST).');
  const response = await fetch(url.replace(/\/$/, ''), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(command),
    cache: 'no-store'
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.error) throw new Error(payload.error || `Redis REST returned HTTP ${response.status}`);
  return payload.result;
}

export function validId(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(value);
}
