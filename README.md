# Local Agent Bridge for Vercel

An authenticated task relay between a Vercel-hosted UI/API and a local agent. The local machine initiates outbound HTTPS polling; Vercel never opens a connection into your home network. This is a task relay, not a raw TCP/WebSocket tunnel.

## What is included

- Vercel/Next.js API routes to enqueue a prompt, let the local bridge claim it, submit a result, and poll task status.
- A minimal browser UI (`/`) protected by a control token.
- A Python 3 polling connector (`tools/local_bridge.py`) which forwards messages to an HTTP endpoint bound to localhost and returns the response.
- Upstash Redis REST for durable, shared queue state across stateless Vercel functions.

The local-agent HTTP endpoint must accept `POST application/json` with `{"message":"..."}` and return JSON with one of `output`, `text`, `content`, or `response`, or plain text. OpenCode and other agents may expose different APIs; in that case, use a small local adapter that translates this contract to the agent's API. The bridge intentionally does not run shell commands or expose a local port to the public internet.

## Deploy

1. In Vercel, open the dedicated `local-agent-bridge` project created for this setup.
2. From this folder, link and deploy with the Vercel CLI (`npm i -g vercel`, then `vercel link --project local-agent-bridge` and `vercel deploy`). For a production URL, use `vercel deploy --prod` after the first successful preview.
3. Add an Upstash Redis database via the Vercel Marketplace, or create one in Upstash. In Project Settings → Environment Variables, set:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `AGENT_TOKEN` (long random secret)
   - `CONTROL_TOKEN` (a different long random secret)
4. Redeploy after configuring the variables. Without Redis, API routes intentionally return a configuration error; Vercel functions must not rely on in-memory state because different requests can run on different instances.
5. Open the deployed URL and enter the `CONTROL_TOKEN` in the UI.

Do not paste secrets into source files, chat, or Git. Keep the two tokens distinct; rotate both if exposed. The task queue and results expire after one hour. Do not put credentials or sensitive user data in prompts unless required.

## Run the local connector

Python 3.9+; no third-party packages required. Configure environment variables in the local shell (never in browser JavaScript):

```sh
export BRIDGE_URL='https://your-deployment.vercel.app'
export AGENT_TOKEN='same secret as Vercel AGENT_TOKEN'
export LOCAL_AGENT_URL='http://127.0.0.1:1234/your-adapter'
# Optional, only if the local agent adapter requires a token:
export LOCAL_AGENT_TOKEN=''
python3 tools/local_bridge.py
```

The connector only accepts a local HTTP URL (`localhost`, `127.0.0.1`, or `::1`) for `LOCAL_AGENT_URL`. Bind your adapter to loopback, not `0.0.0.0`. It will poll every two seconds by default. Stop it with Ctrl+C.

## API contract

- `POST /api/tasks` — `Authorization: Bearer CONTROL_TOKEN`, body `{"prompt":"..."}`; returns a task ID.
- `POST /api/agent/next` — `Authorization: Bearer AGENT_TOKEN`; returns `{ "task": null }` or `{ "task": {"id":"...","prompt":"..."} }`.
- `POST /api/agent/result` — `Authorization: Bearer AGENT_TOKEN`, body `{"taskId":"...","output":"..."}` or `{"taskId":"...","error":"..."}`.
- `GET /api/tasks/{id}` — `Authorization: Bearer CONTROL_TOKEN`; returns task status/result.

Limits: prompts 4 KB, results 16 KB, queue retention one hour. This starter does not implement user accounts, team permissions, streaming responses, or an agent-specific OpenCode adapter. Protect the Vercel deployment with appropriate access controls before sharing it.
