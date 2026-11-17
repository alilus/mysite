import { authorize, redis, sendError } from '../../../lib/relay';

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return sendError(res, 405, 'Use POST.');
  const auth = authorize(req, 'AGENT_TOKEN');
  if (!auth.ok) return sendError(res, auth.status, auth.message);
  try {
    // Skip stale list entries whose task record has expired.
    for (let i = 0; i < 20; i++) {
      const raw = await redis('LPOP', 'lab:queue');
      if (!raw) return res.status(200).json({ task: null });
      const task = JSON.parse(raw);
      const saved = await redis('GET', `lab:task:${task.id}`);
      if (!saved) continue;
      const record = JSON.parse(saved);
      record.status = 'running';
      record.startedAt = new Date().toISOString();
      await redis('SET', `lab:task:${task.id}`, JSON.stringify(record), 'EX', '3600');
      return res.status(200).json({ task });
    }
    return res.status(200).json({ task: null });
  } catch (error) {
    console.error('agent poll failed:', error.message);
    return sendError(res, 503, 'Relay storage is unavailable or not configured.');
  }
}
