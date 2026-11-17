#!/usr/bin/env python3
"""
Local Agent Bridge (GitHub API <-> OpenCode / localhost agent).

Runs on your local machine (Windows, Linux, or macOS) with Python 3.9+ stdlib only.
No third-party packages required. No inbound ports opened — outbound HTTPS to
api.github.com only.

Quick start (Windows PowerShell):
    python tools\\github_bridge.py
    python tools\\github_bridge.py --workdir "C:\\ruta\\a\\tu\\proyecto"
    python tools\\github_bridge.py --doctor
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

PROTOCOL_MARKER = '<!-- lab-v1 -->'
PROTOCOL_VERSION = 1
DEFAULT_REPO = 'alilus/mysite'
DEFAULT_ISSUE = 1
DEFAULT_ALLOWED_USER = 'alilus'
DEFAULT_OPENCODE_URL = 'http://127.0.0.1:4096'
MAX_COMMENT_CHARS = 58000
MAX_RESULT_PARTS = 8
ALLOWED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def configure_console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


def utc_now_iso(offset_seconds: int = 0) -> str:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return now.strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(value: str) -> Optional[dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def log(msg: str, *, err: bool = False) -> None:
    stamp = dt.datetime.now().strftime('%H:%M:%S')
    print(f'[{stamp}] {msg}', file=sys.stderr if err else sys.stdout, flush=True)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


# ---------------------------------------------------------------------------
# Protocol encoding / decoding (supports multi-part chunking up to ~400 KB)
# ---------------------------------------------------------------------------

def encode_envelope(payload: Dict[str, Any]) -> str:
    data = dict(payload)
    data['v'] = PROTOCOL_VERSION
    data.setdefault('ts', utc_now_iso())
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return f'{PROTOCOL_MARKER}\n```json\n{raw}\n```'


def decode_envelope(body: str) -> Optional[Dict[str, Any]]:
    if not isinstance(body, str) or PROTOCOL_MARKER not in body:
        return None
    marker_idx = body.find(PROTOCOL_MARKER)
    start = body.find('{', marker_idx + len(PROTOCOL_MARKER))
    end = body.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get('v') != PROTOCOL_VERSION:
        return None
    return obj


def split_text_for_envelope(
    base_payload: Dict[str, Any],
    field: str,
    text: str,
    max_comment_chars: int = MAX_COMMENT_CHARS,
    max_parts: int = MAX_RESULT_PARTS,
) -> List[Dict[str, Any]]:
    """Split `text` so every encoded comment fits safely under GitHub's 65,536-char limit."""
    probe = dict(base_payload)
    probe[field] = text
    probe['part'] = 1
    probe['total'] = 1
    if len(encode_envelope(probe)) <= max_comment_chars:
        return [probe]

    chunks: List[str] = []
    pos = 0
    n = len(text)
    overhead_probe = dict(base_payload)
    overhead_probe[field] = ''
    overhead_probe['part'] = max_parts
    overhead_probe['total'] = max_parts
    budget = max(1000, max_comment_chars - len(encode_envelope(overhead_probe)) - 256)

    while pos < n:
        if len(chunks) >= max_parts:
            suffix = f'\n\n[... truncado tras {max_parts} partes ...]'
            chunks[-1] = chunks[-1][: max(0, len(chunks[-1]) - len(suffix) - 64)] + suffix
            break
        hi = min(n, pos + budget)
        lo = pos + 1
        candidate = text[pos:hi]
        test = dict(overhead_probe)
        test[field] = candidate
        while len(encode_envelope(test)) > max_comment_chars and hi > lo:
            step = max(256, (hi - pos) // 5)
            hi = max(lo, hi - step)
            candidate = text[pos:hi]
            test[field] = candidate
        chunks.append(candidate)
        pos = hi

    total = len(chunks)
    result: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        item = dict(base_payload)
        item[field] = chunk
        item['part'] = idx
        item['total'] = total
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Environment / GitHub credential discovery (never paste tokens into chat!)
# ---------------------------------------------------------------------------

def load_local_env_files() -> Dict[str, str]:
    found: Dict[str, str] = {}
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir.parent / '.env',
        script_dir.parent / '.env.local',
        Path.cwd() / '.env',
        Path.cwd() / '.env.local',
    ]
    seen: Set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            content = resolved.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, val = line.split('=', 1)
            key = key.strip()
            if key.startswith('export '):
                key = key[7:].strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and val and not val.startswith('replace-with-'):
                found[key] = val
    return found


def get_setting(name: str, file_env: Dict[str, str], default: str = '') -> str:
    val = os.environ.get(name, '').strip()
    if val and not val.startswith('replace-with-'):
        return val
    return file_env.get(name, default).strip()


def find_gh_binary() -> Optional[str]:
    found = shutil.which('gh')
    if found:
        return found
    if os.name == 'nt':
        win_paths = [
            Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'GitHub CLI' / 'gh.exe',
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'GitHub CLI' / 'gh.exe',
            Path(os.environ.get('USERPROFILE', '')) / 'scoop' / 'shims' / 'gh.exe',
        ]
        for p in win_paths:
            if p.is_file():
                return str(p)
    return None


def find_git_binary() -> Optional[str]:
    found = shutil.which('git')
    if found:
        return found
    if os.name == 'nt':
        for base in (
            os.environ.get('ProgramFiles', r'C:\Program Files'),
            os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'),
        ):
            if base:
                candidate = Path(base) / 'Git' / 'cmd' / 'git.exe'
                if candidate.is_file():
                    return str(candidate)
    return None


def iter_github_tokens(file_env: Dict[str, str], interactive_login: bool = False) -> List[Tuple[str, str]]:
    """Return ordered list of (token, source_label) from local env, gh CLI, or Git Credential Manager."""
    results: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    def add(tok: str, label: str) -> None:
        tok = tok.strip()
        if tok and tok not in seen:
            seen.add(tok)
            results.append((tok, label))

    for var in ('BRIDGE_GITHUB_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN'):
        val = get_setting(var, file_env)
        if val:
            add(val, f'variable {var}')

    gh_bin = find_gh_binary()
    if gh_bin:
        try:
            proc = subprocess.run(
                [gh_bin, 'auth', 'token'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                add(proc.stdout.strip(), 'GitHub CLI (gh auth token)')
        except Exception:
            pass

    git_bin = find_git_binary()
    if git_bin:
        env = dict(os.environ)
        if not interactive_login:
            env['GIT_TERMINAL_PROMPT'] = '0'
            env['GCM_INTERACTIVE'] = 'never'
        try:
            proc = subprocess.run(
                [git_bin, 'credential', 'fill'],
                input='protocol=https\nhost=github.com\n\n',
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env,
                timeout=90 if interactive_login else 8,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith('password='):
                        add(line.split('=', 1)[1], 'Git Credential Manager (git)')
                        break
        except Exception:
            pass

    return results


def discover_github_token(file_env: Dict[str, str], interactive_login: bool = False) -> Tuple[str, str]:
    """Return first (token, source_label) from local environment, gh CLI, or Git Credential Manager."""
    candidates = iter_github_tokens(file_env, interactive_login=interactive_login)
    return candidates[0] if candidates else ('', '')


# ---------------------------------------------------------------------------
# GitHub REST API client (with ETag conditional polling)
# ---------------------------------------------------------------------------

class GitHubRelayClient:
    def __init__(self, repo: str, issue: int, token: str, allowed_user: str):
        if '/' not in repo or len(repo.split('/')) != 2:
            raise ValueError(f'BRIDGE_REPO inválido: {repo!r} (usa formato owner/repo)')
        self.repo = repo
        self.issue = int(issue)
        self.token = token
        self.allowed_user = allowed_user.lower()
        self.base = 'https://api.github.com'
        self._etag: Optional[str] = None
        self._etag_since: Optional[str] = None

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> Tuple[int, Any, Dict[str, str]]:
        url = path if path.startswith('https://') else f'{self.base}{path}'
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {self.token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'local-agent-bridge/1.0',
        }
        if body is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(4 * 1024 * 1024)
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                parsed = json.loads(raw.decode('utf-8')) if raw else None
                return resp.status, parsed, resp_headers
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            if exc.code == 304:
                return 304, None, resp_headers
            detail = exc.read(8192).decode('utf-8', errors='replace')
            raise GitHubAPIError(exc.code, detail, resp_headers) from None

    def get_authenticated_user(self) -> str:
        _, data, _ = self._request('GET', '/user')
        return str((data or {}).get('login') or '')

    def get_issue(self) -> Dict[str, Any]:
        _, data, _ = self._request('GET', f'/repos/{self.repo}/issues/{self.issue}')
        return data or {}

    def list_comments(self, since: Optional[str] = None, use_etag: bool = True) -> Optional[List[Dict[str, Any]]]:
        """Return list of comments, or None if 304 Not Modified."""
        params = {'per_page': '100'}
        if since:
            params['since'] = since
        query = urllib.parse.urlencode(params)
        path = f'/repos/{self.repo}/issues/{self.issue}/comments?{query}'

        extra: Dict[str, str] = {}
        if use_etag and self._etag and self._etag_since == since:
            extra['If-None-Match'] = self._etag

        status, data, headers = self._request('GET', path, extra_headers=extra)
        if status == 304:
            return None

        if use_etag:
            self._etag = headers.get('etag')
            self._etag_since = since

        comments: List[Dict[str, Any]] = list(data or [])
        page = 2
        while len(data or []) == 100 and page <= 10:
            params['page'] = str(page)
            p_query = urllib.parse.urlencode(params)
            _, data, _ = self._request('GET', f'/repos/{self.repo}/issues/{self.issue}/comments?{p_query}')
            if not data:
                break
            comments.extend(data)
            page += 1
        return comments

    def post_envelope(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = encode_envelope(payload)
        _, data, _ = self._request(
            'POST',
            f'/repos/{self.repo}/issues/{self.issue}/comments',
            data={'body': body},
        )
        return data or {}

    def is_trusted_comment(self, comment: Dict[str, Any]) -> bool:
        user = comment.get('user') or {}
        login = str(user.get('login') or '').lower()
        assoc = str(comment.get('author_association') or '').upper()
        if login != self.allowed_user:
            return False
        if assoc not in ALLOWED_ASSOCIATIONS:
            return False
        return True


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, detail: str, headers: Dict[str, str]):
        self.status = status
        self.detail = detail
        self.headers = headers
        super().__init__(f'GitHub HTTP {status}: {detail[:400]}')

    def retry_delay(self, default: float = 6.0) -> float:
        retry_after = self.headers.get('retry-after')
        if retry_after and retry_after.isdigit():
            return min(120.0, max(2.0, float(retry_after)))
        if self.status in (403, 429) and self.headers.get('x-ratelimit-remaining') == '0':
            reset = self.headers.get('x-ratelimit-reset')
            if reset and reset.isdigit():
                wait = float(reset) - time.time() + 2.0
                return min(300.0, max(5.0, wait))
        return default


# ---------------------------------------------------------------------------
# Local Agent Runners (OpenCode CLI, OpenCode HTTP Server, Generic HTTP)
# ---------------------------------------------------------------------------

def validate_loopback_http(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError(
            f'Por seguridad, la URL local debe ser http://127.0.0.1 o http://localhost (recibido: {url!r}).'
        )
    return parsed


def find_opencode_binary(explicit: str = '') -> Optional[str]:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return str(p)
        w = shutil.which(explicit)
        if w:
            return w
        return None

    found = shutil.which('opencode')
    if found:
        return found

    candidates: List[Path] = []
    if os.name == 'nt':
        home = Path(os.environ.get('USERPROFILE', str(Path.home())))
        local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData' / 'Local')))
        roaming = Path(os.environ.get('APPDATA', str(home / 'AppData' / 'Roaming')))
        candidates.extend([
            home / '.opencode' / 'bin' / 'opencode.exe',
            local / 'opencode' / 'bin' / 'opencode.exe',
            local / 'Programs' / 'opencode' / 'opencode.exe',
            local / 'Microsoft' / 'WinGet' / 'Links' / 'opencode.exe',
            home / '.bun' / 'bin' / 'opencode.exe',
            home / 'scoop' / 'shims' / 'opencode.exe',
            home / 'scoop' / 'shims' / 'opencode.cmd',
            Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'chocolatey' / 'bin' / 'opencode.exe',
            roaming / 'npm' / 'opencode.cmd',
        ])
    else:
        home = Path.home()
        candidates.extend([
            home / '.opencode' / 'bin' / 'opencode',
            home / '.local' / 'bin' / 'opencode',
            home / '.bun' / 'bin' / 'opencode',
        ])

    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def probe_opencode_http(base_url: str, username: str = 'opencode', password: str = '') -> Optional[str]:
    """Return server version string if an OpenCode v1-compatible HTTP server is healthy at base_url."""
    try:
        validate_loopback_http(base_url)
    except RuntimeError:
        return None
    headers = {'Accept': 'application/json'}
    if password:
        token = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
        headers['Authorization'] = f'Basic {token}'
    url = base_url.rstrip('/') + '/global/health'
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(16384).decode('utf-8', errors='replace'))
            if isinstance(data, dict) and data.get('healthy') is True:
                return str(data.get('version') or 'unknown')
    except Exception:
        return None
    return None


class LocalAgentRunner:
    def __init__(
        self,
        mode: str,
        workdir: Path,
        workdir_explicit: bool = False,
        opencode_bin: Optional[str] = None,
        opencode_url: str = DEFAULT_OPENCODE_URL,
        opencode_url_explicit: bool = False,
        opencode_user: str = 'opencode',
        opencode_password: str = '',
        opencode_auto: bool = False,
        local_agent_url: str = '',
        local_agent_token: str = '',
        default_model: str = '',
        default_agent: str = '',
        timeout: int = 600,
    ):
        self.workdir = workdir.resolve()
        self.workdir_explicit = workdir_explicit
        self.opencode_bin = opencode_bin
        self.opencode_url = opencode_url.rstrip('/')
        self.opencode_url_explicit = opencode_url_explicit
        self.opencode_user = opencode_user or 'opencode'
        self.opencode_password = opencode_password
        self.opencode_auto = opencode_auto
        self.local_agent_url = local_agent_url.strip()
        self.local_agent_token = local_agent_token
        self.default_model = default_model.strip()
        self.default_agent = default_agent.strip()
        self.timeout = max(15, int(timeout))
        self.active_session_id: Optional[str] = None
        self.version: str = ''
        self.cli_supports_format_json: bool = False
        self.cli_supports_auto: bool = False
        self.backend: str = self._resolve_backend(mode)

    def _resolve_backend(self, mode: str) -> str:
        if mode == 'http':
            if not self.local_agent_url:
                raise RuntimeError('Modo "http" requiere LOCAL_AGENT_URL=http://127.0.0.1:<puerto>/...')
            validate_loopback_http(self.local_agent_url)
            return 'http'

        if mode == 'opencode-http':
            url = self.local_agent_url or self.opencode_url
            validate_loopback_http(url)
            self.opencode_url = url.rstrip('/')
            ver = probe_opencode_http(self.opencode_url, self.opencode_user, self.opencode_password)
            if not ver:
                raise RuntimeError(
                    f'No responde OpenCode Server en {self.opencode_url}/global/health. '
                    f'Inícialo con "opencode serve --port 4096" o usa --mode opencode-cli.'
                )
            self.version = ver
            return 'opencode-http'

        if mode == 'opencode-cli':
            if not self.opencode_bin:
                raise RuntimeError(
                    'No se encontró el comando "opencode" en PATH. '
                    'Asegúrate de tener OpenCode instalado o pasa --opencode-bin "C:\\ruta\\opencode.exe".'
                )
            self._inspect_opencode_cli()
            return 'opencode-cli'

        # mode == 'auto'
        if self.local_agent_url:
            validate_loopback_http(self.local_agent_url)
            ver = probe_opencode_http(self.local_agent_url, self.opencode_user, self.opencode_password)
            if ver:
                self.opencode_url = self.local_agent_url.rstrip('/')
                self.version = ver
                return 'opencode-http'
            return 'http'

        if not self.workdir_explicit or self.opencode_url_explicit:
            ver = probe_opencode_http(self.opencode_url, self.opencode_user, self.opencode_password)
            if ver:
                self.version = ver
                return 'opencode-http'

        if self.opencode_bin:
            self._inspect_opencode_cli()
            return 'opencode-cli'

        ver = probe_opencode_http(self.opencode_url, self.opencode_user, self.opencode_password)
        if ver:
            self.version = ver
            return 'opencode-http'

        raise RuntimeError(
            'No se detectó OpenCode ni LOCAL_AGENT_URL.\n'
            '  - Instala o agrega "opencode" al PATH (prueba: opencode --version), o\n'
            '  - Ejecuta "opencode serve --port 4096" en otra terminal, o\n'
            '  - Indica la ruta con: python tools\\github_bridge.py --opencode-bin "C:\\ruta\\opencode.exe", o\n'
            '  - Define LOCAL_AGENT_URL=http://127.0.0.1:<puerto>/tu-endpoint'
        )

    def _inspect_opencode_cli(self) -> None:
        assert self.opencode_bin is not None
        try:
            proc = subprocess.run(
                [self.opencode_bin, '--version'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=15,
            )
            out = strip_ansi((proc.stdout or proc.stderr or '').strip())
            if out:
                self.version = out.splitlines()[-1].strip()[:80]
        except Exception:
            self.version = 'unknown'

        try:
            proc = subprocess.run(
                [self.opencode_bin, 'run', '--help'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=15,
            )
            help_text = (proc.stdout or '') + '\n' + (proc.stderr or '')
            self.cli_supports_format_json = '--format' in help_text
            self.cli_supports_auto = '--auto' in help_text
        except Exception:
            self.cli_supports_format_json = False
            self.cli_supports_auto = False

    def reset_session(self) -> None:
        self.active_session_id = None

    def status_summary(self) -> Dict[str, Any]:
        return {
            'backend': self.backend,
            'opencodeVersion': self.version or None,
            'workdirName': self.workdir.name or 'root',
            'sessionId': self.active_session_id,
            'autoApprove': bool(self.opencode_auto and self.cli_supports_auto) if self.backend == 'opencode-cli' else None,
            'platform': platform.system(),
        }

    def run(
        self,
        prompt: str,
        session: str = 'continue',
        model: str = '',
        agent: str = '',
    ) -> Tuple[str, Optional[str]]:
        chosen_model = (model or self.default_model).strip()
        chosen_agent = (agent or self.default_agent).strip()

        if session == 'new':
            self.active_session_id = None

        if self.backend == 'opencode-http':
            return self._run_opencode_http(prompt, session=session, model=chosen_model, agent=chosen_agent)
        if self.backend == 'opencode-cli':
            return self._run_opencode_cli(prompt, session=session, model=chosen_model, agent=chosen_agent)
        return self._run_generic_http(prompt, session=session, model=chosen_model)

    # -- OpenCode HTTP Server (/session + /session/:id/message) --

    def _opencode_http_request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
        url = self.opencode_url + path
        headers = {'Accept': 'application/json'}
        if self.opencode_password:
            token = base64.b64encode(f'{self.opencode_user}:{self.opencode_password}'.encode('utf-8')).decode('ascii')
            headers['Authorization'] = f'Basic {token}'
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(8 * 1024 * 1024).decode('utf-8', errors='replace')
            return json.loads(raw) if raw.strip() else {}

    def _ensure_http_session(self, session: str) -> str:
        if session and session not in ('continue', 'new'):
            self.active_session_id = session
            return session
        if session == 'continue' and self.active_session_id:
            return self.active_session_id
        created = self._opencode_http_request('POST', '/session', {'title': 'Arena Bridge'}, timeout=30)
        sid = str((created or {}).get('id') or '')
        if not sid:
            raise RuntimeError(f'OpenCode Server no devolvió ID de sesión: {created!r}')
        self.active_session_id = sid
        return sid

    def _run_opencode_http(self, prompt: str, session: str, model: str, agent: str) -> Tuple[str, Optional[str]]:
        sid = self._ensure_http_session(session)
        body: Dict[str, Any] = {'parts': [{'type': 'text', 'text': prompt}]}
        if agent:
            body['agent'] = agent
        if model and '/' in model:
            provider_id, model_id = model.split('/', 1)
            body['model'] = {'providerID': provider_id, 'modelID': model_id}

        try:
            resp = self._opencode_http_request(
                'POST',
                f'/session/{urllib.parse.quote(sid, safe="")}/message',
                body,
                timeout=self.timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and session == 'continue':
                self.active_session_id = None
                sid = self._ensure_http_session('new')
                resp = self._opencode_http_request(
                    'POST',
                    f'/session/{urllib.parse.quote(sid, safe="")}/message',
                    body,
                    timeout=self.timeout,
                )
            else:
                detail = exc.read(4096).decode('utf-8', errors='replace')
                raise RuntimeError(f'OpenCode HTTP {exc.code}: {detail}') from None

        text_parts: List[str] = []
        if isinstance(resp, dict):
            info = resp.get('info') or {}
            for part in resp.get('parts') or []:
                if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                    text_parts.append(part['text'])
            if not text_parts and isinstance(info, dict) and info.get('error'):
                raise RuntimeError(f'Error de OpenCode: {json.dumps(info["error"], ensure_ascii=False)}')
            if text_parts:
                return '\n'.join(text_parts).strip(), sid
            return json.dumps(resp, ensure_ascii=False, indent=2), sid
        return str(resp), sid

    # -- OpenCode CLI (`opencode run` with stdin EOF) --

    def _run_opencode_cli(
        self,
        prompt: str,
        session: str,
        model: str,
        agent: str,
        _allow_session_retry: bool = True,
    ) -> Tuple[str, Optional[str]]:
        assert self.opencode_bin is not None
        target_session: Optional[str] = None
        if session and session not in ('continue', 'new'):
            target_session = session
        elif session == 'continue' and self.active_session_id:
            target_session = self.active_session_id

        use_json = self.cli_supports_format_json
        cmd: List[str] = [self.opencode_bin, 'run']
        if use_json:
            cmd.extend(['--format', 'json'])
        if self.opencode_auto and self.cli_supports_auto:
            cmd.append('--auto')
        if target_session:
            cmd.extend(['--session', target_session])
        if agent:
            cmd.extend(['--agent', agent])
        if model:
            cmd.extend(['--model', model])

        env = dict(os.environ)
        env.setdefault('NO_COLOR', '1')

        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=str(self.workdir),
            env=env,
            timeout=self.timeout,
        )

        # Fallback for older OpenCode builds that require a positional prompt argument
        stderr_clean = strip_ansi(proc.stderr or '').strip()
        stdout_clean = strip_ansi(proc.stdout or '').strip()
        combined_lower = (stderr_clean + '\n' + stdout_clean).lower()
        if (
            proc.returncode != 0
            and ('not enough non-option arguments' in combined_lower or 'missing required argument' in combined_lower)
            and not self.opencode_bin.lower().endswith(('.cmd', '.bat'))
        ):
            proc = subprocess.run(
                cmd + [prompt],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(self.workdir),
                env=env,
                timeout=self.timeout,
            )
            stderr_clean = strip_ansi(proc.stderr or '').strip()
            stdout_clean = strip_ansi(proc.stdout or '').strip()

        # Retry without --session if a stale session ID expired/was deleted
        if (
            proc.returncode != 0
            and target_session
            and session == 'continue'
            and _allow_session_retry
            and 'session' in combined_lower
            and ('not found' in combined_lower or 'does not exist' in combined_lower)
        ):
            self.active_session_id = None
            return self._run_opencode_cli(prompt, session='new', model=model, agent=agent, _allow_session_retry=False)

        if use_json:
            text, sid, errors = parse_opencode_ndjson(proc.stdout or '')
            if sid:
                self.active_session_id = sid
            if text:
                return text, self.active_session_id or target_session
            if errors:
                raise RuntimeError('Error de OpenCode: ' + '; '.join(errors))

        if proc.returncode != 0:
            detail = stderr_clean or stdout_clean or f'Código de salida {proc.returncode}'
            raise RuntimeError(f'opencode run falló ({proc.returncode}): {detail[:4000]}')

        output = stdout_clean or stderr_clean or '(OpenCode terminó sin salida de texto)'
        return output, self.active_session_id or target_session

    # -- Generic loopback HTTP endpoint (also supports OpenAI /v1/chat/completions) --

    def _run_generic_http(self, prompt: str, session: str, model: str) -> Tuple[str, Optional[str]]:
        validate_loopback_http(self.local_agent_url)
        headers = {'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json'}
        if self.local_agent_token:
            headers['Authorization'] = f'Bearer {self.local_agent_token}'

        is_openai = self.local_agent_url.rstrip('/').endswith('/chat/completions')
        if is_openai:
            payload: Dict[str, Any] = {'messages': [{'role': 'user', 'content': prompt}]}
            if model:
                payload['model'] = model
        else:
            payload = {'message': prompt, 'session': session}

        req = urllib.request.Request(
            self.local_agent_url,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(4 * 1024 * 1024).decode('utf-8', errors='replace')

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip(), None

        if isinstance(obj, dict):
            sid = obj.get('sessionId') if isinstance(obj.get('sessionId'), str) else None
            if sid:
                self.active_session_id = sid
            for key in ('output', 'text', 'content', 'response', 'reply'):
                val = obj.get(key)
                if isinstance(val, str):
                    return val, sid
            choices = obj.get('choices')
            if isinstance(choices, list) and choices:
                msg = (choices[0] or {}).get('message') or {}
                if isinstance(msg.get('content'), str):
                    return msg['content'], sid
        return json.dumps(obj, ensure_ascii=False, indent=2), None


def parse_opencode_ndjson(raw: str) -> Tuple[str, Optional[str], List[str]]:
    """Parse newline-delimited JSON output from `opencode run --format json` (v1 & v2)."""
    parts_text: List[str] = []
    delta_buf: List[str] = []
    errors: List[str] = []
    session_id: Optional[str] = None
    parsed_any = False
    plain_lines: List[str] = []

    def flush_deltas() -> None:
        if delta_buf:
            joined = ''.join(delta_buf).strip()
            if joined:
                parts_text.append(joined)
            delta_buf.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith('{'):
            plain_lines.append(strip_ansi(line))
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            plain_lines.append(strip_ansi(line))
            continue
        if not isinstance(ev, dict):
            continue
        parsed_any = True

        for skey in ('sessionID', 'sessionId', 'session_id'):
            sval = ev.get(skey)
            if isinstance(sval, str) and sval:
                session_id = sval
                break

        etype = str(ev.get('type') or '')
        part = ev.get('part')

        if isinstance(ev.get('delta'), str) and etype in ('text', 'text_delta', 'message.delta'):
            delta_buf.append(ev['delta'])
        elif etype == 'text':
            flush_deltas()
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                parts_text.append(part['text'])
            elif isinstance(ev.get('text'), str):
                parts_text.append(ev['text'])
        elif etype in ('assistant', 'result', 'output') and isinstance(ev.get('text'), str):
            flush_deltas()
            parts_text.append(ev['text'])
        elif etype == 'error':
            flush_deltas()
            err_obj = ev.get('error') or ev.get('message') or ev
            if isinstance(err_obj, str):
                errors.append(err_obj)
            elif isinstance(err_obj, dict):
                msg = (
                    (err_obj.get('data') or {}).get('message')
                    if isinstance(err_obj.get('data'), dict)
                    else None
                ) or err_obj.get('message') or json.dumps(err_obj, ensure_ascii=False)
                errors.append(str(msg))
            else:
                errors.append(json.dumps(err_obj, ensure_ascii=False))

    flush_deltas()
    cleaned = [t.strip() for t in parts_text if t and t.strip()]
    if cleaned:
        return '\n\n'.join(cleaned), session_id, errors
    if not parsed_any and plain_lines:
        return '\n'.join(plain_lines).strip(), session_id, errors
    return '', session_id, errors


# ---------------------------------------------------------------------------
# Main Bridge Polling Loop
# ---------------------------------------------------------------------------

class BridgeService:
    def __init__(
        self,
        gh: GitHubRelayClient,
        runner: LocalAgentRunner,
        poll_seconds: float = 2.0,
        lookback_minutes: int = 15,
        announce_ready: bool = True,
    ):
        self.gh = gh
        self.runner = runner
        self.poll_seconds = max(1.0, min(30.0, float(poll_seconds)))
        self.lookback_minutes = max(0, int(lookback_minutes))
        self.announce_ready = announce_ready
        self.bridge_id = uuid.uuid4().hex[:8]
        self.seen_comment_ids: Set[int] = set()
        self.handled_task_ids: Set[str] = set()
        self.task_buffers: Dict[str, Dict[str, Any]] = {}
        self.since: str = utc_now_iso(-max(60, self.lookback_minutes * 60))

    def bootstrap(self) -> None:
        """Scan recent comments to avoid re-running already-handled tasks, then pick up fresh unanswered ones."""
        comments = self.gh.list_comments(since=self.since, use_etag=True) or []
        pending_tasks: List[Tuple[str, Dict[str, Any]]] = []
        latest_created: Optional[dt.datetime] = None

        for comment in comments:
            cid = int(comment.get('id') or 0)
            if cid:
                self.seen_comment_ids.add(cid)
            created = parse_iso(str(comment.get('updated_at') or comment.get('created_at') or ''))
            if created and (latest_created is None or created > latest_created):
                latest_created = created
            if not self.gh.is_trusted_comment(comment):
                continue
            env = decode_envelope(str(comment.get('body') or ''))
            if not env:
                continue
            mtype = env.get('type')
            if mtype in ('ack', 'result'):
                tid = str(env.get('taskId') or '')
                if tid:
                    self.handled_task_ids.add(tid)
            elif mtype == 'task':
                assembled = self._collect_task_part(env)
                if assembled is not None:
                    pending_tasks.append((str(assembled['id']), assembled))

        if latest_created:
            self._advance_since(latest_created)

        if self.announce_ready:
            ready_payload = {
                'type': 'ready',
                'bridgeId': self.bridge_id,
                **self.runner.status_summary(),
            }
            created_comment = self.gh.post_envelope(ready_payload)
            cid = int(created_comment.get('id') or 0)
            if cid:
                self.seen_comment_ids.add(cid)

        if self.lookback_minutes > 0:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=self.lookback_minutes)
            for tid, task in pending_tasks:
                if tid in self.handled_task_ids:
                    continue
                task_ts = parse_iso(str(task.get('ts') or ''))
                if task_ts and task_ts < cutoff:
                    self.handled_task_ids.add(tid)
                    continue
                self._execute_task(task)
        else:
            for tid, _ in pending_tasks:
                self.handled_task_ids.add(tid)

    def _advance_since(self, latest_created: dt.datetime) -> None:
        safe = latest_created - dt.timedelta(seconds=2)
        new_since = safe.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if new_since > self.since:
            self.since = new_since

    def _collect_task_part(self, env: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tid = str(env.get('id') or '').strip()
        if not tid or tid in self.handled_task_ids:
            return None
        total = int(env.get('total') or 1)
        part = int(env.get('part') or 1)
        if total <= 1:
            return env
        if total > 16 or part < 1 or part > total:
            return None
        buf = self.task_buffers.setdefault(tid, {'total': total, 'parts': {}, 'base': env})
        buf['parts'][part] = str(env.get('prompt') or '')
        if part == 1:
            buf['base'] = env
        if len(buf['parts']) == buf['total']:
            full_prompt = ''.join(buf['parts'][i] for i in range(1, buf['total'] + 1))
            merged = dict(buf['base'])
            merged['prompt'] = full_prompt
            merged['part'] = 1
            merged['total'] = 1
            self.task_buffers.pop(tid, None)
            return merged
        return None

    def poll_once(self) -> int:
        comments = self.gh.list_comments(since=self.since, use_etag=True)
        if comments is None:
            return 0  # 304 Not Modified — 0 rate limit used

        latest_created: Optional[dt.datetime] = None
        new_tasks: List[Dict[str, Any]] = []

        for comment in comments:
            cid = int(comment.get('id') or 0)
            created = parse_iso(str(comment.get('updated_at') or comment.get('created_at') or ''))
            if created and (latest_created is None or created > latest_created):
                latest_created = created
            if cid in self.seen_comment_ids:
                continue
            if cid:
                self.seen_comment_ids.add(cid)
                if len(self.seen_comment_ids) > 4000:
                    # Keep recent IDs only
                    self.seen_comment_ids = set(sorted(self.seen_comment_ids)[-2000:])
            if not self.gh.is_trusted_comment(comment):
                continue
            env = decode_envelope(str(comment.get('body') or ''))
            if not env:
                continue
            mtype = env.get('type')
            if mtype in ('ack', 'result'):
                tid = str(env.get('taskId') or '')
                if tid:
                    self.handled_task_ids.add(tid)
            elif mtype == 'task':
                assembled = self._collect_task_part(env)
                if assembled is not None:
                    new_tasks.append(assembled)

        if latest_created:
            self._advance_since(latest_created)

        executed = 0
        for task in new_tasks:
            tid = str(task.get('id') or '')
            if tid and tid not in self.handled_task_ids:
                self._execute_task(task)
                executed += 1
        return executed

    def _post_and_remember(self, payload: Dict[str, Any]) -> None:
        created = self.gh.post_envelope(payload)
        cid = int(created.get('id') or 0)
        if cid:
            self.seen_comment_ids.add(cid)

    def _execute_task(self, task: Dict[str, Any]) -> None:
        tid = str(task.get('id') or '')
        kind = str(task.get('kind') or 'prompt')
        prompt = str(task.get('prompt') or '')
        session = str(task.get('session') or 'continue')
        model = str(task.get('model') or '')
        agent = str(task.get('agent') or '')

        self.handled_task_ids.add(tid)
        started = time.monotonic()

        if kind == 'ping':
            log(f'Ping recibido ({tid[:8]}) -> respondiendo estado...')
            info = {'pong': True, 'bridgeId': self.bridge_id, **self.runner.status_summary()}
            self._post_and_remember({
                'type': 'result',
                'taskId': tid,
                'bridgeId': self.bridge_id,
                'ok': True,
                'output': json.dumps(info, ensure_ascii=False),
                'sessionId': self.runner.active_session_id,
                'durationMs': int((time.monotonic() - started) * 1000),
                'part': 1,
                'total': 1,
            })
            log(f'Pong enviado ({tid[:8]}).')
            return

        if kind == 'reset_session':
            self.runner.reset_session()
            self._post_and_remember({
                'type': 'result',
                'taskId': tid,
                'bridgeId': self.bridge_id,
                'ok': True,
                'output': 'Sesión local reiniciada.',
                'sessionId': None,
                'durationMs': int((time.monotonic() - started) * 1000),
                'part': 1,
                'total': 1,
            })
            log(f'Sesión reiniciada por solicitud ({tid[:8]}).')
            return

        preview = prompt.replace('\n', ' ')[:90]
        log(f'Mensaje recibido ({tid[:8]}): {preview!r}')

        try:
            self._post_and_remember({
                'type': 'ack',
                'taskId': tid,
                'bridgeId': self.bridge_id,
                'backend': self.runner.backend,
            })
        except Exception as exc:
            log(f'Aviso: no se pudo publicar ack ({exc})', err=True)

        try:
            output, sid = self.runner.run(prompt, session=session, model=model, agent=agent)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            base = {
                'type': 'result',
                'taskId': tid,
                'bridgeId': self.bridge_id,
                'ok': True,
                'sessionId': sid,
                'durationMs': elapsed_ms,
            }
            envelopes = split_text_for_envelope(base, 'output', output)
            for env in envelopes:
                self._post_and_remember(env)
            log(
                f'Respuesta enviada ({tid[:8]}) en {elapsed_ms / 1000:.1f}s '
                f'({len(output)} chars, {len(envelopes)} parte(s)).'
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            err_text = str(exc)
            log(f'Error en tarea {tid[:8]}: {err_text}', err=True)
            base = {
                'type': 'result',
                'taskId': tid,
                'bridgeId': self.bridge_id,
                'ok': False,
                'sessionId': self.runner.active_session_id,
                'durationMs': elapsed_ms,
            }
            for env in split_text_for_envelope(base, 'error', err_text, max_parts=2):
                self._post_and_remember(env)

    def run_forever(self) -> int:
        self.bootstrap()
        log(
            f'Puente ACTIVO (id={self.bridge_id}) escuchando {self.gh.repo}#{self.gh.issue} '
            f'cada {self.poll_seconds:g}s. Pulsa Ctrl+C para detener.'
        )
        while True:
            try:
                self.poll_once()
                time.sleep(self.poll_seconds)
            except KeyboardInterrupt:
                log('Deteniendo puente local. ¡Hasta luego!')
                return 0
            except GitHubAPIError as exc:
                delay = exc.retry_delay(default=min(self.poll_seconds * 3, 15.0))
                log(f'{exc} — reintentando en {delay:.0f}s', err=True)
                time.sleep(delay)
            except Exception as exc:
                delay = min(self.poll_seconds * 3, 15.0)
                log(f'Error de red: {exc} — reintentando en {delay:.0f}s', err=True)
                time.sleep(delay)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def print_auth_help(repo: str) -> None:
    print(
        '\nNo se encontró una sesión activa de GitHub en esta PC.\n'
        'Configúrala localmente (NUNCA pegues tokens en el chat) con UNA de estas opciones:\n\n'
        '  Opción A (más fácil, con GitHub CLI en PowerShell):\n'
        '    winget install --id GitHub.cli\n'
        '    gh auth login\n\n'
        '  Opción B (archivo .env.local en tu PC, ignorado por git):\n'
        f'    Crea un Fine-Grained Token en GitHub solo para "{repo}" con permiso "Issues: Read and write"\n'
        '    y guárdalo en tu PC en .env.local:\n'
        '      BRIDGE_GITHUB_TOKEN=github_pat_...\n',
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Conector local GitHub API <-> OpenCode (sin abrir puertos entrantes).'
    )
    p.add_argument('--repo', default='', help=f'Repositorio owner/repo (default: {DEFAULT_REPO})')
    p.add_argument('--issue', type=int, default=0, help=f'Número de Issue del canal (default: {DEFAULT_ISSUE})')
    p.add_argument('--allowed-user', default='', help=f'Usuario GitHub autorizado (default: {DEFAULT_ALLOWED_USER})')
    p.add_argument(
        '--mode',
        choices=('auto', 'opencode-cli', 'opencode-http', 'http'),
        default='',
        help='Cómo hablar con tu agente local (default: auto)',
    )
    p.add_argument('--workdir', default='', help='Carpeta del proyecto donde trabajará OpenCode (default: carpeta actual)')
    p.add_argument('--opencode-bin', default='', help='Ruta a opencode.exe si no está en PATH')
    p.add_argument('--opencode-url', default='', help=f'URL de opencode serve (default: {DEFAULT_OPENCODE_URL})')
    p.add_argument(
        '--opencode-auto',
        action='store_true',
        help='Pasar --auto a opencode run para auto-aprobar permisos no denegados',
    )
    p.add_argument('--local-url', default='', help='URL HTTP local alternativa (http://127.0.0.1:puerto/...)')
    p.add_argument('--model', default='', help='Modelo opcional para OpenCode (ej. provider/model)')
    p.add_argument('--agent', default='', help='Agente opcional de OpenCode (ej. build o plan)')
    p.add_argument('--poll', type=float, default=0.0, help='Segundos entre consultas (default: 2)')
    p.add_argument('--timeout', type=int, default=0, help='Tiempo máx. por respuesta del agente en segundos (default: 600)')
    p.add_argument(
        '--lookback-minutes',
        type=int,
        default=15,
        help='Responder tareas sin contestar de los últimos N minutos al iniciar (default: 15, 0 para desactivar)',
    )
    p.add_argument('--no-announce', action='store_true', help='No publicar evento "ready" al iniciar')
    p.add_argument('--login', action='store_true', help='Permitir inicio de sesión interactivo con Git Credential Manager')
    p.add_argument('--doctor', action='store_true', help='Solo verificar conexión a GitHub y OpenCode, y salir')
    p.add_argument('--once', action='store_true', help='Procesar tareas pendientes una vez y salir (útil para pruebas)')
    return p


def main(argv: Optional[List[str]] = None) -> int:
    configure_console_utf8()
    args = build_parser().parse_args(argv)
    file_env = load_local_env_files()

    repo = args.repo or get_setting('BRIDGE_REPO', file_env, DEFAULT_REPO)
    issue = args.issue or int(get_setting('BRIDGE_ISSUE', file_env, str(DEFAULT_ISSUE)))
    allowed_user = args.allowed_user or get_setting('BRIDGE_ALLOWED_USER', file_env, DEFAULT_ALLOWED_USER)
    mode = args.mode or get_setting('AGENT_MODE', file_env, 'auto')
    workdir_explicit = bool(args.workdir or get_setting('OPENCODE_WORKDIR', file_env, ''))
    workdir_str = args.workdir or get_setting('OPENCODE_WORKDIR', file_env, str(Path.cwd()))
    opencode_bin_cfg = args.opencode_bin or get_setting('OPENCODE_BIN', file_env, '')
    opencode_url_explicit = bool(args.opencode_url or get_setting('OPENCODE_SERVER_URL', file_env, ''))
    opencode_url = args.opencode_url or get_setting('OPENCODE_SERVER_URL', file_env, DEFAULT_OPENCODE_URL)
    opencode_user = get_setting('OPENCODE_SERVER_USERNAME', file_env, 'opencode')
    opencode_password = get_setting('OPENCODE_SERVER_PASSWORD', file_env, '')
    opencode_auto = args.opencode_auto or get_setting('OPENCODE_AUTO', file_env, '').lower() in ('1', 'true', 'yes')
    local_url = args.local_url or get_setting('LOCAL_AGENT_URL', file_env, '')
    local_token = get_setting('LOCAL_AGENT_TOKEN', file_env, '')
    model = args.model or get_setting('OPENCODE_MODEL', file_env, '')
    agent = args.agent or get_setting('OPENCODE_AGENT', file_env, '')
    poll_seconds = args.poll or float(get_setting('POLL_SECONDS', file_env, '2'))
    timeout = args.timeout or int(get_setting('AGENT_TIMEOUT', file_env, '600'))

    workdir = Path(workdir_str).expanduser()
    if not workdir.is_dir():
        log(f'La carpeta --workdir no existe: {workdir}', err=True)
        return 2

    log('=== Local Agent Bridge (GitHub API <-> OpenCode) ===')

    # 1. Verify GitHub authentication (try local env, gh CLI, and Git Credential Manager)
    candidates = iter_github_tokens(file_env, interactive_login=args.login)
    if not candidates:
        print_auth_help(repo)
        return 2

    gh: Optional[GitHubRelayClient] = None
    login = ''
    token_source = ''
    last_auth_error: Optional[Exception] = None

    for cand_token, cand_source in candidates:
        cand_gh = GitHubRelayClient(repo=repo, issue=issue, token=cand_token, allowed_user=allowed_user)
        try:
            cand_login = cand_gh.get_authenticated_user()
            gh = cand_gh
            login = cand_login
            token_source = cand_source
            break
        except GitHubAPIError as exc:
            if exc.status == 403 and 'integration' in exc.detail.lower():
                gh = cand_gh
                login = ''
                token_source = cand_source
                break
            last_auth_error = exc
        except Exception as exc:
            log(f'No se pudo conectar a api.github.com: {exc}', err=True)
            return 2

    if gh is None:
        log(f'Falló la autenticación en GitHub: {last_auth_error}', err=True)
        print_auth_help(repo)
        return 2

    if login:
        log(f'GitHub OK: conectado como @{login} (vía {token_source})')
        if login.lower() != allowed_user.lower():
            log(
                f'El usuario autenticado (@{login}) no coincide con el usuario autorizado (@{allowed_user}).',
                err=True,
            )
            return 2
    else:
        log(f'GitHub OK: token de integración detectado (vía {token_source})')

    try:
        issue_data = gh.get_issue()
    except GitHubAPIError as exc:
        log(
            f'No se pudo leer el Issue {repo}#{issue}: {exc}\n'
            f'Verifica que el token tenga permiso "Issues: Read and write" en {repo}.',
            err=True,
        )
        return 2

    locked_note = 'bloqueado a externos' if issue_data.get('locked') else 'ABIERTO a comentarios públicos'
    log(f'Canal OK: {repo}#{issue} ("{issue_data.get("title", "")}") [{locked_note}]')

    # 2. Verify Local Agent (OpenCode / HTTP)
    opencode_bin = find_opencode_binary(opencode_bin_cfg)
    try:
        runner = LocalAgentRunner(
            mode=mode,
            workdir=workdir,
            workdir_explicit=workdir_explicit,
            opencode_bin=opencode_bin,
            opencode_url=opencode_url,
            opencode_url_explicit=opencode_url_explicit,
            opencode_user=opencode_user,
            opencode_password=opencode_password,
            opencode_auto=opencode_auto,
            local_agent_url=local_url,
            local_agent_token=local_token,
            default_model=model,
            default_agent=agent,
            timeout=timeout,
        )
    except RuntimeError as exc:
        log(str(exc), err=True)
        return 2

    extra_info = f' (v{runner.version})' if runner.version else ''
    if runner.backend == 'opencode-cli':
        log(f'Agente OK: OpenCode CLI{extra_info} -> {runner.opencode_bin}')
        log(f'Proyecto : {runner.workdir}')
    elif runner.backend == 'opencode-http':
        log(f'Agente OK: OpenCode Server{extra_info} -> {runner.opencode_url}')
    else:
        log(f'Agente OK: Endpoint HTTP local -> {runner.local_agent_url}')

    if args.doctor:
        log('Diagnóstico completado: TODO LISTO para recibir y responder mensajes.')
        return 0

    service = BridgeService(
        gh=gh,
        runner=runner,
        poll_seconds=poll_seconds,
        lookback_minutes=args.lookback_minutes,
        announce_ready=not args.no_announce,
    )

    if args.once:
        service.bootstrap()
        service.poll_once()
        return 0

    return service.run_forever()


if __name__ == '__main__':
    raise SystemExit(main())
