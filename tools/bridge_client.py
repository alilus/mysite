#!/usr/bin/env python3
"""
Cloud / Arena Client for Local Agent Bridge (GitHub API).

Allows the Arena agent (or any authorized environment with GitHub access) to:
  - Send messages/tasks to the user's local OpenCode agent and wait for the reply
  - Ping the local bridge to verify live two-way connectivity
  - Check bridge status and conversation history
  - Resume waiting for a pending task
  - Initialize/lock or clean up the dedicated GitHub Issue channel

Examples:
    python3 tools/bridge_client.py status
    python3 tools/bridge_client.py ping
    python3 tools/bridge_client.py send "Explica la estructura de este proyecto"
    python3 tools/bridge_client.py send --new-session "Empieza una revisión nueva"
    python3 tools/bridge_client.py send --cleanup "Responde con hola"
    python3 tools/bridge_client.py wait <task_id>
    python3 tools/bridge_client.py history --limit 10
    python3 tools/bridge_client.py cleanup
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Allow running both as `python3 tools/bridge_client.py` and from inside `tools/`
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from github_bridge import (  # noqa: E402
    DEFAULT_ALLOWED_USER,
    DEFAULT_ISSUE,
    DEFAULT_REPO,
    GitHubAPIError,
    GitHubRelayClient,
    configure_console_utf8,
    decode_envelope,
    discover_github_token,
    get_setting,
    load_local_env_files,
    parse_iso,
    split_text_for_envelope,
    utc_now_iso,
)

BRIDGE_ISSUE_TITLE = 'Local Agent Bridge Channel'
BRIDGE_ISSUE_BODY = (
    'Canal dedicado de relay entre el agente en Arena y el agente local (OpenCode).\n\n'
    '- Solo acepta mensajes autenticados del propietario del repositorio.\n'
    '- Conversación bloqueada para usuarios externos.\n'
    '- **Seguridad:** no envíes contraseñas, tokens ni claves privadas por este canal.'
)


def status_log(msg: str) -> None:
    stamp = dt.datetime.now().strftime('%H:%M:%S')
    print(f'[{stamp}] {msg}', file=sys.stderr, flush=True)


def make_client(args: argparse.Namespace) -> GitHubRelayClient:
    file_env = load_local_env_files()
    repo = getattr(args, 'repo', '') or get_setting('BRIDGE_REPO', file_env, DEFAULT_REPO)
    issue = getattr(args, 'issue', 0) or int(get_setting('BRIDGE_ISSUE', file_env, str(DEFAULT_ISSUE)))
    allowed_user = getattr(args, 'allowed_user', '') or get_setting(
        'BRIDGE_ALLOWED_USER', file_env, DEFAULT_ALLOWED_USER
    )
    token, _ = discover_github_token(file_env)
    if not token:
        raise RuntimeError(
            'No se encontró token de GitHub (GH_TOKEN / GITHUB_TOKEN / gh auth token).'
        )
    return GitHubRelayClient(repo=repo, issue=issue, token=token, allowed_user=allowed_user)


def delete_comment(gh: GitHubRelayClient, comment_id: int) -> None:
    gh._request('DELETE', f'/repos/{gh.repo}/issues/comments/{int(comment_id)}')


def send_task(
    gh: GitHubRelayClient,
    prompt: str,
    kind: str = 'prompt',
    session: str = 'continue',
    model: str = '',
    agent: str = '',
) -> Tuple[str, List[int], str]:
    """Post a task (chunked if large). Returns (task_id, posted_comment_ids, since_iso)."""
    task_id = str(uuid.uuid4())
    since_iso = utc_now_iso(-5)
    base: Dict[str, Any] = {
        'type': 'task',
        'id': task_id,
        'kind': kind,
        'session': session,
    }
    if model:
        base['model'] = model
    if agent:
        base['agent'] = agent

    envelopes = split_text_for_envelope(base, 'prompt', prompt)
    posted_ids: List[int] = []
    for env in envelopes:
        created = gh.post_envelope(env)
        cid = int(created.get('id') or 0)
        if cid:
            posted_ids.append(cid)
    return task_id, posted_ids, since_iso


def wait_for_task(
    gh: GitHubRelayClient,
    task_id: str,
    since_iso: Optional[str] = None,
    ack_timeout: float = 0.0,
    timeout: float = 600.0,
    poll_seconds: float = 2.0,
    quiet: bool = False,
) -> Tuple[Dict[str, Any], List[int]]:
    """
    Poll GitHub Issue comments until all result chunks for `task_id` arrive.
    Returns (result_dict, related_comment_ids).
    """
    since = since_iso or utc_now_iso(-3600)
    seen_ids: Set[int] = set()
    related_comment_ids: List[int] = []
    acked = False
    result_parts: Dict[int, str] = {}
    result_total: Optional[int] = None
    result_meta: Dict[str, Any] = {}

    start = time.monotonic()
    deadline = start + max(5.0, float(timeout))
    ack_deadline = (start + float(ack_timeout)) if ack_timeout and ack_timeout > 0 else None

    while time.monotonic() < deadline:
        try:
            comments = gh.list_comments(since=since, use_etag=True)
        except GitHubAPIError as exc:
            delay = exc.retry_delay(default=min(poll_seconds * 3, 12.0))
            if not quiet:
                status_log(f'Aviso API GitHub: {exc} — reintentando en {delay:.0f}s')
            time.sleep(delay)
            continue
        except Exception as exc:
            if not quiet:
                status_log(f'Aviso de red: {exc} — reintentando...')
            time.sleep(min(poll_seconds * 2, 10.0))
            continue

        if comments is not None:
            latest_ts: Optional[dt.datetime] = None
            for comment in comments:
                cid = int(comment.get('id') or 0)
                stamp = parse_iso(str(comment.get('updated_at') or comment.get('created_at') or ''))
                if stamp and (latest_ts is None or stamp > latest_ts):
                    latest_ts = stamp
                if cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                if not gh.is_trusted_comment(comment):
                    continue
                env = decode_envelope(str(comment.get('body') or ''))
                if not env:
                    continue

                mtype = env.get('type')
                if mtype == 'task' and env.get('id') == task_id:
                    if cid and cid not in related_comment_ids:
                        related_comment_ids.append(cid)
                elif mtype == 'ack' and env.get('taskId') == task_id:
                    if cid and cid not in related_comment_ids:
                        related_comment_ids.append(cid)
                    if not acked:
                        acked = True
                        if not quiet:
                            backend = env.get('backend') or 'local'
                            bridge_id = env.get('bridgeId') or '?'
                            status_log(
                                f'Recibido por agente local (bridge={bridge_id}, modo={backend}). '
                                f'Esperando respuesta...'
                            )
                elif mtype == 'result' and env.get('taskId') == task_id:
                    if cid and cid not in related_comment_ids:
                        related_comment_ids.append(cid)
                    acked = True
                    part = int(env.get('part') or 1)
                    total = int(env.get('total') or 1)
                    ok = bool(env.get('ok', 'error' not in env))
                    field = 'output' if ok else 'error'
                    result_parts[part] = str(env.get(field) or '')
                    result_total = total
                    if part == 1 or not result_meta:
                        result_meta = dict(env)

            if result_total is not None and len(result_parts) >= result_total:
                ok = bool(result_meta.get('ok', True))
                full_text = ''.join(result_parts.get(i, '') for i in range(1, result_total + 1))
                final = {
                    'taskId': task_id,
                    'ok': ok,
                    'output': full_text if ok else None,
                    'error': None if ok else full_text,
                    'bridgeId': result_meta.get('bridgeId'),
                    'sessionId': result_meta.get('sessionId'),
                    'durationMs': result_meta.get('durationMs'),
                    'ts': result_meta.get('ts'),
                }
                return final, related_comment_ids

            if latest_ts:
                safe = (latest_ts - dt.timedelta(seconds=2)).astimezone(dt.timezone.utc)
                new_since = safe.strftime('%Y-%m-%dT%H:%M:%SZ')
                if new_since > since:
                    since = new_since

        if not acked and ack_deadline is not None and time.monotonic() >= ack_deadline:
            raise TimeoutError(
                f'El puente local aún no acusó recibo tras {ack_timeout:g}s (taskId={task_id}). '
                f'Verifica que `python tools\\github_bridge.py` esté corriendo en tu PC. '
                f'La tarea sigue en cola y puedes esperarla con: '
                f'`python3 tools/bridge_client.py wait {task_id}`'
            )

        time.sleep(poll_seconds)

    state = 'en ejecución en tu PC' if acked else 'sin recoger (puente local desconectado)'
    raise TimeoutError(
        f'Tiempo de espera agotado ({timeout:g}s) para taskId={task_id} [{state}]. '
        f'Usa `python3 tools/bridge_client.py wait {task_id}` para seguir esperando.'
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init_issue(args: argparse.Namespace) -> int:
    file_env = load_local_env_files()
    repo = args.repo or get_setting('BRIDGE_REPO', file_env, DEFAULT_REPO)
    allowed_user = args.allowed_user or get_setting('BRIDGE_ALLOWED_USER', file_env, DEFAULT_ALLOWED_USER)
    token, _ = discover_github_token(file_env)
    if not token:
        print('Error: no se encontró token de GitHub.', file=sys.stderr)
        return 2

    gh = GitHubRelayClient(repo=repo, issue=args.issue or DEFAULT_ISSUE, token=token, allowed_user=allowed_user)

    # Search open issues for an existing bridge channel
    query = urllib.parse.urlencode({'state': 'open', 'per_page': '50'})
    _, issues, _ = gh._request('GET', f'/repos/{repo}/issues?{query}')
    target = None
    for item in issues or []:
        if 'pull_request' in item:
            continue
        if args.issue and item.get('number') == args.issue:
            target = item
            break
        if not args.issue and item.get('title') == BRIDGE_ISSUE_TITLE:
            target = item
            break

    if not target:
        _, target, _ = gh._request(
            'POST',
            f'/repos/{repo}/issues',
            data={'title': BRIDGE_ISSUE_TITLE, 'body': BRIDGE_ISSUE_BODY},
        )
        print(f'Issue creado: #{target["number"]} ({target["html_url"]})')
    else:
        print(f'Issue existente: #{target["number"]} ({target["html_url"]})')

    num = int(target['number'])
    if not target.get('locked'):
        gh._request('PUT', f'/repos/{repo}/issues/{num}/lock', data={'lock_reason': 'resolved'})
        print(f'Issue #{num} bloqueado para usuarios externos (solo @{allowed_user} puede comentar).')
    else:
        print(f'Issue #{num} ya está bloqueado para usuarios externos.')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    gh = make_client(args)
    issue = gh.get_issue()
    since = utc_now_iso(-24 * 3600)
    comments = gh.list_comments(since=since, use_etag=False) or []

    last_ready: Optional[Dict[str, Any]] = None
    last_activity: Optional[Dict[str, Any]] = None
    tasks: Dict[str, Dict[str, Any]] = {}
    acked: Set[str] = set()
    completed: Set[str] = set()

    for c in comments:
        if not gh.is_trusted_comment(c):
            continue
        env = decode_envelope(str(c.get('body') or ''))
        if not env:
            continue
        mtype = env.get('type')
        if mtype == 'ready':
            last_ready = env
            last_activity = env
        elif mtype == 'ack':
            acked.add(str(env.get('taskId') or ''))
            last_activity = env
        elif mtype == 'result':
            completed.add(str(env.get('taskId') or ''))
            last_activity = env
        elif mtype == 'task':
            tid = str(env.get('id') or '')
            if tid and int(env.get('part') or 1) == 1:
                tasks[tid] = env

    pending = [t for tid, t in tasks.items() if tid not in completed]
    info = {
        'repo': gh.repo,
        'issue': gh.issue,
        'url': issue.get('html_url'),
        'state': issue.get('state'),
        'lockedToOwner': bool(issue.get('locked')),
        'totalComments': issue.get('comments'),
        'lastBridgeReady': last_ready,
        'lastBridgeActivity': {
            'type': last_activity.get('type'),
            'ts': last_activity.get('ts'),
            'bridgeId': last_activity.get('bridgeId'),
            'sessionId': last_activity.get('sessionId'),
        } if last_activity else None,
        'pendingTasks': [
            {
                'id': t.get('id'),
                'kind': t.get('kind', 'prompt'),
                'ts': t.get('ts'),
                'status': 'running' if t.get('id') in acked else 'queued',
                'promptPreview': str(t.get('prompt') or '')[:80],
            }
            for t in pending
        ],
    }

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f'Canal        : {info["repo"]}#{info["issue"]} ({info["url"]})')
    print(f'Estado Issue : {info["state"]} | Bloqueado a externos: {"sí" if info["lockedToOwner"] else "NO"}')
    print(f'Comentarios  : {info["totalComments"]}')
    if last_ready:
        print(
            f'Último ready : {last_ready.get("ts")} | bridge={last_ready.get("bridgeId")} '
            f'| backend={last_ready.get("backend")} | v={last_ready.get("opencodeVersion")} '
            f'| carpeta={last_ready.get("workdirName")} | SO={last_ready.get("platform")}'
        )
    else:
        print('Último ready : (ninguno en las últimas 24h — inicia tools\\github_bridge.py en tu PC)')
    if last_activity and last_activity is not last_ready:
        print(
            f'Última activ.: {last_activity.get("ts")} ({last_activity.get("type")}, '
            f'session={last_activity.get("sessionId")})'
        )
    print(f'Pendientes   : {len(pending)}')
    for item in info['pendingTasks']:
        print(f'  - [{item["status"]}] {item["id"]} ({item["ts"]}): {item["promptPreview"]!r}')
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    gh = make_client(args)
    task_id, posted_ids, since_iso = send_task(gh, prompt='ping', kind='ping')
    if not args.json:
        status_log(f'Ping enviado (taskId={task_id}). Esperando al puente local...')
    try:
        result, related_ids = wait_for_task(
            gh,
            task_id=task_id,
            since_iso=since_iso,
            timeout=args.timeout,
            poll_seconds=args.poll,
            quiet=args.json,
        )
    except TimeoutError as exc:
        if args.cleanup:
            for cid in posted_ids:
                try:
                    delete_comment(gh, cid)
                except Exception:
                    pass
        print(str(exc), file=sys.stderr)
        return 1

    if args.cleanup:
        all_ids = set(posted_ids) | set(related_ids)
        for cid in all_ids:
            try:
                delete_comment(gh, cid)
            except Exception:
                pass

    print(result.get('output') or json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


def cmd_send(args: argparse.Namespace) -> int:
    gh = make_client(args)

    if args.file:
        prompt = Path(args.file).read_text(encoding='utf-8')
    elif args.stdin:
        prompt = sys.stdin.read()
    else:
        prompt = ' '.join(args.prompt or []).strip()

    if not prompt.strip():
        print('Error: debes indicar un mensaje, --file o --stdin.', file=sys.stderr)
        return 2

    session = 'new' if args.new_session else (args.session or 'continue')
    kind = 'reset_session' if args.reset_session else 'prompt'

    task_id, posted_ids, since_iso = send_task(
        gh,
        prompt=prompt,
        kind=kind,
        session=session,
        model=args.model or '',
        agent=args.agent or '',
    )

    if args.no_wait:
        if args.json:
            print(json.dumps({'taskId': task_id, 'status': 'queued'}, ensure_ascii=False))
        else:
            print(f'Tarea enviada (taskId={task_id}). Consulta luego con: python3 tools/bridge_client.py wait {task_id}')
        return 0

    if not args.json:
        status_log(f'Mensaje enviado a {gh.repo}#{gh.issue} (taskId={task_id}). Esperando agente local...')

    try:
        result, related_ids = wait_for_task(
            gh,
            task_id=task_id,
            since_iso=since_iso,
            ack_timeout=args.ack_timeout,
            timeout=args.timeout,
            poll_seconds=args.poll,
            quiet=args.json,
        )
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.cleanup:
        for cid in set(posted_ids) | set(related_ids):
            try:
                delete_comment(gh, cid)
            except Exception:
                pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok') else 1

    if result.get('ok'):
        dur = result.get('durationMs')
        sid = result.get('sessionId')
        meta_bits = []
        if dur is not None:
            meta_bits.append(f'{dur / 1000:.1f}s')
        if sid:
            meta_bits.append(f'session={sid}')
        if meta_bits:
            status_log(f'Respuesta recibida ({", ".join(meta_bits)}):')
        print(result.get('output') or '')
        return 0

    status_log('El agente local devolvió un error:')
    print(result.get('error') or 'Error desconocido', file=sys.stderr)
    return 1


def cmd_wait(args: argparse.Namespace) -> int:
    gh = make_client(args)
    since_iso = utc_now_iso(-max(60, args.lookback_minutes * 60))
    if not args.json:
        status_log(f'Esperando respuesta de taskId={args.task_id}...')
    try:
        result, related_ids = wait_for_task(
            gh,
            task_id=args.task_id,
            since_iso=since_iso,
            timeout=args.timeout,
            poll_seconds=args.poll,
            quiet=args.json,
        )
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.cleanup:
        for cid in related_ids:
            try:
                delete_comment(gh, cid)
            except Exception:
                pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok') else 1

    if result.get('ok'):
        print(result.get('output') or '')
        return 0
    print(result.get('error') or 'Error desconocido', file=sys.stderr)
    return 1


def cmd_reset_session(args: argparse.Namespace) -> int:
    gh = make_client(args)
    task_id, posted_ids, since_iso = send_task(gh, prompt='reset', kind='reset_session', session='new')
    status_log(f'Solicitud de reinicio de sesión enviada (taskId={task_id})...')
    try:
        result, related_ids = wait_for_task(gh, task_id=task_id, since_iso=since_iso, timeout=args.timeout, poll_seconds=args.poll)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.cleanup:
        for cid in set(posted_ids) | set(related_ids):
            try:
                delete_comment(gh, cid)
            except Exception:
                pass
    print(result.get('output') or 'OK')
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    gh = make_client(args)
    comments = gh.list_comments(since=utc_now_iso(-7 * 24 * 3600), use_etag=False) or []
    tasks: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for c in comments:
        if not gh.is_trusted_comment(c):
            continue
        env = decode_envelope(str(c.get('body') or ''))
        if not env:
            continue
        mtype = env.get('type')
        if mtype == 'task':
            tid = str(env.get('id') or '')
            if not tid:
                continue
            entry = tasks.setdefault(tid, {'id': tid, 'kind': env.get('kind', 'prompt'), 'ts': env.get('ts'), 'prompt_parts': {}, 'result_parts': {}, 'status': 'queued'})
            if tid not in order:
                order.append(tid)
            entry['prompt_parts'][int(env.get('part') or 1)] = str(env.get('prompt') or '')
        elif mtype == 'ack':
            tid = str(env.get('taskId') or '')
            if tid in tasks and tasks[tid]['status'] == 'queued':
                tasks[tid]['status'] = 'running'
        elif mtype == 'result':
            tid = str(env.get('taskId') or '')
            if tid in tasks:
                ok = bool(env.get('ok', True))
                tasks[tid]['status'] = 'completed' if ok else 'error'
                field = 'output' if ok else 'error'
                tasks[tid]['result_parts'][int(env.get('part') or 1)] = str(env.get(field) or '')
                tasks[tid]['sessionId'] = env.get('sessionId')

    recent = order[-max(1, args.limit):]
    if not recent:
        print('No hay mensajes recientes en el canal.')
        return 0

    for tid in recent:
        t = tasks[tid]
        prompt = ''.join(v for _, v in sorted(t['prompt_parts'].items()))
        reply = ''.join(v for _, v in sorted(t['result_parts'].items()))
        print(f'=== [{t["status"]}] {tid} ({t.get("ts")}) kind={t["kind"]} ===')
        print(f'>> ENVIADO:\n{prompt[:600]}{"..." if len(prompt) > 600 else ""}')
        if reply:
            print(f'<< RESPUESTA:\n{reply[:1000]}{"..." if len(reply) > 1000 else ""}')
        print()
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    gh = make_client(args)
    comments = gh.list_comments(since=None, use_etag=False) or []
    proto_comments = [
        c for c in comments
        if decode_envelope(str(c.get('body') or '')) is not None
    ]
    keep = max(0, int(args.keep))
    to_delete = proto_comments[:-keep] if keep > 0 else proto_comments
    deleted = 0
    for c in to_delete:
        cid = int(c.get('id') or 0)
        if not cid:
            continue
        delete_comment(gh, cid)
        deleted += 1
    print(f'Limpieza completada: {deleted} comentario(s) eliminados, {len(proto_comments) - deleted} conservados.')
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--repo', default='', help=f'Repositorio owner/repo (default: {DEFAULT_REPO})')
    common.add_argument('--issue', type=int, default=0, help=f'Issue del canal (default: {DEFAULT_ISSUE})')
    common.add_argument('--allowed-user', default='', help=f'Usuario autorizado (default: {DEFAULT_ALLOWED_USER})')

    p = argparse.ArgumentParser(description='Cliente para enviar y recibir mensajes del agente local vía GitHub API.')
    sub = p.add_subparsers(dest='command', required=True)

    sp_init = sub.add_parser('init-issue', parents=[common], help='Crear y bloquear el Issue del canal')
    sp_init.set_defaults(func=cmd_init_issue)

    sp_status = sub.add_parser('status', parents=[common], help='Ver estado del canal y del puente local')
    sp_status.add_argument('--json', action='store_true', help='Salida en JSON')
    sp_status.set_defaults(func=cmd_status)

    sp_ping = sub.add_parser('ping', parents=[common], help='Comprobar conexión en vivo con el puente local')
    sp_ping.add_argument('--timeout', type=float, default=30.0, help='Segundos de espera (default: 30)')
    sp_ping.add_argument('--poll', type=float, default=1.5, help='Intervalo de polling (default: 1.5)')
    sp_ping.add_argument('--cleanup', action='store_true', help='Borrar comentarios del ping al terminar')
    sp_ping.add_argument('--json', action='store_true', help='Salida silenciosa solo JSON')
    sp_ping.set_defaults(func=cmd_ping)

    sp_send = sub.add_parser('send', parents=[common], help='Enviar un mensaje al agente local y esperar respuesta')
    sp_send.add_argument('prompt', nargs='*', help='Mensaje para el agente local')
    sp_send.add_argument('--file', default='', help='Leer mensaje desde un archivo')
    sp_send.add_argument('--stdin', action='store_true', help='Leer mensaje desde stdin')
    sp_send.add_argument('--new-session', action='store_true', help='Iniciar una sesión nueva en OpenCode')
    sp_send.add_argument('--session', default='', help='Continuar un ID de sesión específico de OpenCode')
    sp_send.add_argument('--model', default='', help='Modelo opcional (provider/model)')
    sp_send.add_argument('--agent', default='', help='Agente opcional de OpenCode (build, plan, etc.)')
    sp_send.add_argument('--ack-timeout', type=float, default=25.0, help='Segundos máx. esperando acuse de recibo (0=sin límite)')
    sp_send.add_argument('--timeout', type=float, default=600.0, help='Segundos máx. esperando respuesta total (default: 600)')
    sp_send.add_argument('--poll', type=float, default=2.0, help='Intervalo de polling en segundos (default: 2)')
    sp_send.add_argument('--no-wait', action='store_true', help='Solo encolar mensaje sin esperar respuesta')
    sp_send.add_argument('--cleanup', action='store_true', help='Eliminar comentarios de esta tarea tras leer la respuesta')
    sp_send.add_argument('--reset-session', action='store_true', help=argparse.SUPPRESS)
    sp_send.add_argument('--json', action='store_true', help='Imprimir resultado estructurado en JSON')
    sp_send.set_defaults(func=cmd_send)

    sp_wait = sub.add_parser('wait', parents=[common], help='Esperar la respuesta de una tarea ya enviada')
    sp_wait.add_argument('task_id', help='UUID de la tarea')
    sp_wait.add_argument('--timeout', type=float, default=600.0, help='Segundos máx. de espera (default: 600)')
    sp_wait.add_argument('--poll', type=float, default=2.0, help='Intervalo de polling (default: 2)')
    sp_wait.add_argument('--lookback-minutes', type=int, default=120, help='Ventana de búsqueda hacia atrás en minutos')
    sp_wait.add_argument('--cleanup', action='store_true', help='Eliminar comentarios de la tarea tras recibirla')
    sp_wait.add_argument('--json', action='store_true', help='Salida en JSON')
    sp_wait.set_defaults(func=cmd_wait)

    sp_reset = sub.add_parser('reset-session', parents=[common], help='Reiniciar la sesión activa de OpenCode')
    sp_reset.add_argument('--timeout', type=float, default=30.0)
    sp_reset.add_argument('--poll', type=float, default=1.5)
    sp_reset.add_argument('--cleanup', action='store_true')
    sp_reset.set_defaults(func=cmd_reset_session)

    sp_hist = sub.add_parser('history', parents=[common], help='Mostrar historial reciente de mensajes')
    sp_hist.add_argument('--limit', type=int, default=10, help='Cantidad de tareas a mostrar')
    sp_hist.set_defaults(func=cmd_history)

    sp_clean = sub.add_parser('cleanup', parents=[common], help='Eliminar mensajes del Issue para limpiar el canal')
    sp_clean.add_argument('--keep', type=int, default=0, help='Conservar los últimos N comentarios (default: 0)')
    sp_clean.set_defaults(func=cmd_cleanup)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    configure_console_utf8()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print('\nCancelado.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
