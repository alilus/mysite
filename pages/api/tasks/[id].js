import { authorize, redis, sendError, validId } from '../../../lib/relay';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'GET') return sendError(res, 405, 'Use GET.');
  const auth = authorize(req, 'CONTROL_TOKEN');
  if (!auth.ok) return sendError(res, auth.status, auth.message);
  const { id } = req.query;
  if (!validId(id)) return sendError(res, 400, 'Invalid task id.');
  try {
    const raw = await redis('GET', `lab:task:${id}`);
    if (!raw) return sendError(res, 404, 'Task expired or not found.');
    return res.status(200).json(JSON.parse(raw));
  } catch (error) {
    console.error('task lookup failed:', error.message);
    return sendError(res, 503, 'Relay storage is unavailable or not configured.');
  }
}
