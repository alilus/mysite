import { useEffect, useState } from 'react';

const box = { background: '#101820', color: '#e8eef5', minHeight: '100vh', fontFamily: 'system-ui, sans-serif', padding: '32px 18px' };
const panel = { maxWidth: 760, margin: '0 auto', background: '#17232e', border: '1px solid #2b3c4b', borderRadius: 16, padding: 24 };
const field = { width: '100%', boxSizing: 'border-box', padding: 12, borderRadius: 9, border: '1px solid #405465', background: '#0d151c', color: '#eef5fa', margin: '6px 0 16px' };

export default function Home() {
  const [token, setToken] = useState('');
  const [prompt, setPrompt] = useState('');
  const [task, setTask] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => { setToken(sessionStorage.getItem('control-token') || ''); }, []);

  async function submit(e) {
    e.preventDefault(); setBusy(true); setMessage(''); setTask(null);
    sessionStorage.setItem('control-token', token);
    try {
      const r = await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ prompt }) });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      setTask({ id: data.id, status: data.status });
      setMessage('Trabajo enviado. Esperando al agente local…');
      poll(data.id, token);
    } catch (err) { setMessage(err.message); setBusy(false); }
  }

  async function poll(id, authToken) {
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1800));
      try {
        const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`, { headers: { Authorization: `Bearer ${authToken}` }, cache: 'no-store' });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        setTask(data);
        if (data.status === 'completed') { setMessage('Respuesta recibida.'); setBusy(false); return; }
        if (data.status === 'error') { setMessage('El agente devolvió un error.'); setBusy(false); return; }
      } catch (err) { setMessage(err.message); setBusy(false); return; }
    }
    setMessage('Se agotó la espera. Si el agente sigue trabajando, vuelve a consultar con el ID de tarea.');
    setBusy(false);
  }

  return <main style={box}><section style={panel}>
    <div style={{ color: '#80d6b5', fontWeight: 700, letterSpacing: 1 }}>LOCAL AGENT RELAY</div>
    <h1 style={{ margin: '8px 0' }}>Vercel ↔ agente local</h1>
    <p style={{ color: '#b5c3cf', lineHeight: 1.5 }}>La PC mantiene conexiones salientes consultando la cola; Vercel no abre una conexión entrante a tu red. Los mensajes se guardan temporalmente y expiran al cabo de una hora.</p>
    <form onSubmit={submit}>
      <label htmlFor="token">Token de control</label>
      <input id="token" type="password" autoComplete="off" style={field} value={token} onChange={e => setToken(e.target.value)} required />
      <label htmlFor="prompt">Mensaje para el agente</label>
      <textarea id="prompt" rows="5" maxLength={4000} style={{ ...field, resize: 'vertical' }} value={prompt} onChange={e => setPrompt(e.target.value)} required />
      <button disabled={busy || !token || !prompt.trim()} style={{ padding: '11px 18px', border: 0, borderRadius: 9, fontWeight: 700, background: busy ? '#61746d' : '#80d6b5', color: '#0b1b17', cursor: busy ? 'wait' : 'pointer' }}>{busy ? 'Esperando…' : 'Enviar tarea'}</button>
    </form>
    {message && <p role="status" style={{ marginTop: 18, color: '#d8e4ec' }}>{message}</p>}
    {task && <div style={{ marginTop: 18, padding: 14, borderRadius: 10, background: '#0d151c', border: '1px solid #304352' }}>
      <div><b>Estado:</b> {task.status}</div><div style={{ wordBreak: 'break-all', fontSize: 12, color: '#9eafbd' }}>ID: {task.id}</div>
      {task.output && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.5 }}>{task.output}</pre>}
      {task.error && <pre style={{ whiteSpace: 'pre-wrap', color: '#ff9d9d' }}>{task.error}</pre>}
    </div>}
    <hr style={{ borderColor: '#2b3c4b', margin: '24px 0' }} />
    <p style={{ color: '#9eafbd', fontSize: 13, lineHeight: 1.5 }}>Requiere configurar Redis REST y dos secretos en Vercel. El token de control se guarda solo en la sesión de este navegador. No envíes contraseñas, claves API ni datos privados al agente si no son necesarios.</p>
  </section></main>;
}
