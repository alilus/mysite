#!/usr/bin/env python3
"""Polling connector for the Vercel relay. Does not execute shell commands."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BRIDGE_URL = os.environ.get('BRIDGE_URL', '').rstrip('/')
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')
LOCAL_AGENT_URL = os.environ.get('LOCAL_AGENT_URL', '')
LOCAL_AGENT_TOKEN = os.environ.get('LOCAL_AGENT_TOKEN', '')
POLL_SECONDS = max(1, min(30, int(os.environ.get('POLL_SECONDS', '2'))))


def request_json(url, data=None, bearer=None, timeout=30):
    body = None if data is None else json.dumps(data).encode('utf-8')
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    req = urllib.request.Request(url, data=body, headers=headers, method='GET' if data is None else 'POST')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read(256 * 1024)
        return response.status, json.loads(payload.decode('utf-8')) if payload else {}


def local_agent(prompt):
    parsed = urllib.parse.urlparse(LOCAL_AGENT_URL)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('LOCAL_AGENT_URL must be an http://localhost or 127.0.0.1 URL; use a reviewed local adapter for other setups.')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if LOCAL_AGENT_TOKEN:
        headers['Authorization'] = 'Bearer ' + LOCAL_AGENT_TOKEN
    body = json.dumps({'message': prompt}).encode('utf-8')
    req = urllib.request.Request(LOCAL_AGENT_URL, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=600) as response:
        raw = response.read(64 * 1024).decode('utf-8', errors='replace')
        try:
            obj = json.loads(raw)
            for key in ('output', 'text', 'content', 'response'):
                value = obj.get(key) if isinstance(obj, dict) else None
                if isinstance(value, str):
                    return value[:16000]
            return json.dumps(obj, ensure_ascii=False)[:16000]
        except json.JSONDecodeError:
            return raw[:16000]


def report(task_id, output=None, error=None):
    data = {'taskId': task_id}
    if error is not None:
        data['error'] = str(error)[:16000]
    else:
        data['output'] = str(output)[:16000]
    request_json(BRIDGE_URL + '/api/agent/result', data, AGENT_TOKEN, timeout=30)


def main():
    if not (BRIDGE_URL.startswith('https://') and AGENT_TOKEN and LOCAL_AGENT_URL):
        print('Set BRIDGE_URL=https://<your-vercel-domain>, AGENT_TOKEN and LOCAL_AGENT_URL=http://127.0.0.1:<port>/<adapter>.')
        return 2
    print('Local bridge running. Press Ctrl+C to stop. Do not share AGENT_TOKEN.')
    while True:
        try:
            _, data = request_json(BRIDGE_URL + '/api/agent/next', {}, AGENT_TOKEN, timeout=30)
            task = data.get('task')
            if not task:
                time.sleep(POLL_SECONDS)
                continue
            task_id = task.get('id')
            print('Received task', task_id)
            try:
                output = local_agent(task.get('prompt', ''))
                report(task_id, output=output)
                print('Task completed', task_id)
            except Exception as exc:
                report(task_id, error=exc)
                print('Task failed', task_id, '-', exc, file=sys.stderr)
        except KeyboardInterrupt:
            print('\nStopping local bridge.')
            return 0
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode('utf-8', errors='replace')
            print('Relay HTTP error:', exc.code, detail, file=sys.stderr)
            time.sleep(min(POLL_SECONDS * 3, 15))
        except Exception as exc:
            print('Relay connection error:', exc, file=sys.stderr)
            time.sleep(min(POLL_SECONDS * 3, 15))


if __name__ == '__main__':
    raise SystemExit(main())
