import { randomUUID } from 'node:crypto';
import { authorize, redis, sendError, TTL_SECONDS } from '../../../lib/relay';

export const config = { api: { bodyParser: { sizeLimit: '8kb' } } };

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return sendError(res, 405, 'Use POST.');
  const auth = authorize(req, 'CONTROL_TOKEN');
  if (!auth.ok) return sendError(res, auth.status, auth.message);

  const prompt = typeof req.body?.prompt === 'string' ? req.body.prompt.trim() : '';
  if (!prompt) return sendError(res, 400, 'prompt is required.');
  if (prompt.length > 4000) return sendError(res, 413, 'prompt must be 4000 characters or less.');

  const id = randomUUID();
  const task = { id, prompt, status: 'queued', createdAt: new Date().toISOString() };
  try {
    await redis('SET', `lab:task:${id}`, JSON.stringify(task), 'EX', String(TTL_SECONDS));
    await redis('RPUSH', 'lab:queue', JSON.stringify({ id, prompt }));
    await redis('EXPIRE', 'lab:queue', String(TTL_SECONDS));
    return res.status(202).json({ id, status: task.status });
  } catch (error) {
    console.error('task enqueue failed:', error.message);
    return sendError(res, 503, 'Relay storage is unavailable or not configured.');
  }
}
