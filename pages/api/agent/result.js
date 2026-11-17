import { authorize, redis, sendError, validId } from '../../../lib/relay';

export const config = { api: { bodyParser: { sizeLimit: '24kb' } } };

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return sendError(res, 405, 'Use POST.');
  const auth = authorize(req, 'AGENT_TOKEN');
  if (!auth.ok) return sendError(res, auth.status, auth.message);

  const { taskId, output, error } = req.body || {};
  if (!validId(taskId)) return sendError(res, 400, 'Invalid taskId.');
  if (typeof output !== 'string' && typeof error !== 'string') return sendError(res, 400, 'output or error is required.');
  const text = typeof output === 'string' ? output : error;
  if (text.length > 16000) return sendError(res, 413, 'Result must be 16000 characters or less.');

  try {
    const raw = await redis('GET', `lab:task:${taskId}`);
    if (!raw) return sendError(res, 404, 'Task expired or not found.');
    const record = JSON.parse(raw);
    record.status = typeof error === 'string' ? 'error' : 'completed';
    if (record.status === 'error') record.error = text;
    else record.output = text;
    record.completedAt = new Date().toISOString();
    await redis('SET', `lab:task:${taskId}`, JSON.stringify(record), 'EX', '3600');
    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('agent result write failed:', err.message);
    return sendError(res, 503, 'Relay storage is unavailable or not configured.');
  }
}
