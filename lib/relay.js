import { timingSafeEqual } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

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

// Single Node process only. Render's default filesystem is ephemeral.
// Synchronous read/modify/write avoids interleaving within that process.
export async function redis(command, key, ...args) {
  const file = process.env.RELAY_FILE || '/tmp/local-agent-bridge/relay.json';
  let data;
  try { data = JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') data = {}; else throw error; }
  const now = Date.now();
  for (const [k, entry] of Object.entries(data)) {
    if (entry.expiresAt && entry.expiresAt <= now) delete data[k];
  }
  let result;
  switch (command) {
    case 'GET': result = data[key]?.value ?? null; break;
    case 'SET':
      data[key] = { value: args[0], expiresAt: args[1] === 'EX' ? now + Number(args[2]) * 1000 : null };
      result = 'OK'; break;
    case 'RPUSH':
      if (!data[key]) data[key] = { value: [], expiresAt: null };
      if (!Array.isArray(data[key].value)) throw new Error('Queue has wrong type');
      result = data[key].value.push(args[0]); break;
    case 'LPOP':
      if (data[key] && !Array.isArray(data[key].value)) throw new Error('Queue has wrong type');
      result = data[key]?.value.shift() ?? null; break;
    case 'EXPIRE':
      result = data[key] ? 1 : 0;
      if (data[key]) data[key].expiresAt = now + Number(args[0]) * 1000;
      break;
    default: throw new Error('Unsupported storage operation: ' + command);
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data), { mode: 0o600 });
  fs.renameSync(tmp, file);
  return result;
}

export function validId(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(value);
}
