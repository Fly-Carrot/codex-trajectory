"""Register an exact Codex chat with one shared local viewer. No default popup."""
import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

try:
    from .broker import resolve_session, private_directory, read_private, write_private, SERVICE_SCHEMA
except ImportError:
    from broker import resolve_session, private_directory, read_private, write_private, SERVICE_SCHEMA

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / '.knowledgeos-local/trace-viewer/shared'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Never forward bearer credentials through environment proxies or redirects.
HTTP = build_opener(ProxyHandler({}), NoRedirect())


def request(origin, path, token, payload=None):
    parsed = urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Not a local viewer origin')
    body = None if payload is None else json.dumps(payload).encode()
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    with HTTP.open(Request(origin + path, data=body, headers=headers), timeout=1) as response:
        return json.load(response)


def service_identity(data):
    nonce = secrets.token_hex(32)
    try:
        proof = request(data['origin'], '/api/identity?nonce='+nonce, '')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(proof, dict):
        return False
    expected = hmac.new(data['token'].encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return proof.get('instance') == data['instance'] and hmac.compare_digest(str(proof.get('proof', '')), expected)


def service_health(state, sessions):
    try:
        data = read_private(state / 'service.json')
        if not service_identity(data):
            return None
        actual = request(data['origin'], '/api/health', data['token'])
        if actual.get('service') != 'trajectory' or actual.get('schema') != SERVICE_SCHEMA or actual.get('instance') != data.get('instance') or actual.get('pid') != data.get('pid'):
            return None
        if actual.get('sessions_root') != str(Path(sessions).resolve()):
            raise ValueError('Running service belongs to another sessions directory')
        return data
    except (OSError, KeyError, TypeError):
        return None


def healthy(connection, thread, log=None):
    try:
        data = read_private(connection)
        url = urlsplit(data['url'])
        if data.get('thread_id') != thread or url.path != '/t/' + thread or not url.fragment:
            return None
        origin = url._replace(path='', query='', fragment='').geturl()
        service = read_private(Path(connection).parent.parent/'service.json')
        if service['origin'] != origin or data.get('instance') != service['instance'] or not service_identity(service):
            return None
        actual = request(origin, '/api/threads/' + thread + '/status', url.fragment)
        if actual.get('thread_id') != thread or actual.get('instance') != service['instance'] or actual.get('pid') != service['pid']:
            return None
        if log is not None and actual.get('log') != str(Path(log).resolve()):
            return None
        return {**data, 'instance': actual.get('instance'), 'pid': actual.get('pid', data.get('pid'))}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def open_browser(url):
    subprocess.run(['/usr/bin/open', '-g', url], check=True, timeout=5,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(payload, state=DEFAULT_STATE, sessions=None, auto_open=False, opener=open_browser):
    sessions = sessions or Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions'
    thread, log = resolve_session(payload, sessions)
    state = private_directory(state)
    directory = private_directory(state / thread)
    fd = os.open(state / 'launch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return {'status': 'launch_in_progress', 'thread_id': thread}
                time.sleep(.05)
        service = service_health(state, sessions)
        reused = service is not None
        if service is None:
            child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('broker.py')),
                                      '--sessions', str(sessions), '--state-dir', str(state)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('Viewer not started; saved port may be occupied or state invalid. No other process was stopped.')
                service = service_health(state, sessions)
                if service:
                    break
                time.sleep(.05)
            if not service:
                child.terminate()
                child.wait(timeout=2)
                raise RuntimeError('Viewer startup timed out')
            threading.Thread(target=child.wait, daemon=True).start()
        data = request(service['origin'], '/api/register', service['token'],
                       {'hook_event_name': payload['hook_event_name'], 'session_id': thread, 'transcript_path': str(log)})
        connection = directory / 'connection.json'
        write_private(connection, data)
        if not healthy(connection, thread, log):
            raise RuntimeError('Registered link did not verify')
        opened = False
        stamp = directory / 'opened.json'
        try:
            previous = read_private(stamp)
        except FileNotFoundError:
            previous = None
        # Optional manual convenience only. No hook or global rule is installed.
        if auto_open and previous != {'url': data['url']}:
            opener(data['url'])
            write_private(stamp, {'url': data['url']})
            opened = True
        return {'status': 'reused' if reused else 'started', 'thread_id': thread,
                'opened': opened, 'connection_file': str(connection), 'pid': data['pid'], 'url': data['url']}


def validate_thread(thread):
    if not isinstance(thread, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}', thread):
        raise ValueError('An exact thread ID is required')


def resume(state, thread, sessions=None):
    validate_thread(thread)
    state = Path(state)
    sessions = Path(sessions or Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions').resolve(strict=True)

    def check_directory(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe private state directory')

    check_directory(state)
    try:
        registry = read_private(state / 'registry.json')
    except FileNotFoundError:
        # Legacy connection is a source pointer, never evidence of a live URL.
        check_directory(state / thread)
        entry = read_private(state / thread / 'connection.json')
        if not isinstance(entry, dict) or entry.get('thread_id') != thread:
            raise ValueError('Connection does not match the thread')
    else:
        if (not isinstance(registry, dict) or registry.get('schema') != SERVICE_SCHEMA
                or registry.get('sessions_root') != str(sessions)
                or not isinstance(registry.get('threads'), dict)):
            raise ValueError('Invalid registry')
        for key, value in registry['threads'].items():
            validate_thread(key)
            if (not isinstance(value, dict) or not isinstance(value.get('log'), str)
                    or not value['log'] or not isinstance(value.get('token'), str)
                    or not value['token'] or not isinstance(value.get('logs', []), list)
                    or any(not isinstance(path, str) or not path for path in value.get('logs', []))):
                raise ValueError('Malformed registry entry')
        entry = registry['threads'].get(thread)
    if not isinstance(entry, dict) or not isinstance(entry.get('log'), str) or not entry['log']:
        raise ValueError('No registered source for this exact thread')
    payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': thread,
               'transcript_path': entry['log']}
    # Prefer the current source, then only previously registered segments.
    # Persisted paths are canonical; never follow a replacement symlink.
    for source in dict.fromkeys([entry['log'], *reversed(entry.get('logs', []))]):
        path = Path(source)
        try:
            if path.resolve(strict=True) != path:
                raise ValueError('Registered source path changed')
            _, verified = resolve_session({**payload, 'transcript_path': str(path)}, sessions)
            if verified != path:
                raise ValueError('Registered source path changed')
        except (OSError, ValueError, TypeError):
            continue
        payload['transcript_path'] = str(verified)
        break
    else:
        raise ValueError('No available registered source for this exact thread')
    result = launch(payload, state=state, sessions=sessions, auto_open=False)
    if not result.get('url'):
        raise RuntimeError('Resume did not produce a verified link; retry after the active launch')
    return result


def current_link(state, thread):
    validate_thread(thread)
    data = healthy(Path(state) / thread / 'connection.json', thread)
    if not data:
        raise RuntimeError('No verified live link for this thread. Register this exact transcript first.')
    return {'status': 'available', 'thread_id': thread, 'url': data['url']}


def hook_response(payload, result):
    response = {'suppressOutput': True}
    if payload.get('hook_event_name') == 'UserPromptSubmit' and result.get('url'):
        response['hookSpecificOutput'] = {
            'hookEventName': 'UserPromptSubmit',
            'additionalContext': 'codex-trajectory verified this exact chat. Append this single link at the end of your final reply: '
                                 '[Trajectory](' + result['url'] + '). Do not open a tab automatically. '
                                 'This private local link must not appear in public files. Observer failures must not block work.'}
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hook', action='store_true')
    parser.add_argument('--auto-open', action='store_true')
    parser.add_argument('--url', action='store_true', help='Return a verified existing link; do not start or scan logs')
    parser.add_argument('--resume', action='store_true', help='Recover an exact registered thread without opening a tab')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--thread-id')
    args = parser.parse_args()
    if sum((args.hook, args.url, args.resume)) > 1:
        parser.error('--hook, --url and --resume are mutually exclusive')
    if (args.url or args.resume) and (args.auto_open or args.log is not None):
        parser.error('--url and --resume cannot be combined with --auto-open or --log')
    if (args.url or args.resume) and not args.thread_id:
        parser.error('--url and --resume require --thread-id')
    if args.hook and (args.log is not None or args.thread_id is not None):
        parser.error('--hook cannot be combined with --log or --thread-id')
    try:
        if args.url:
            result = current_link(args.state_dir, args.thread_id)
        elif args.resume:
            result = resume(args.state_dir, args.thread_id)
        else:
            payload = json.loads(sys.stdin.read(1024 * 1024)) if args.hook else {
                'hook_event_name': 'UserPromptSubmit', 'session_id': args.thread_id,
                'transcript_path': str(args.log or '')}
            result = launch(payload, state=args.state_dir, auto_open=args.auto_open)
        print(json.dumps(hook_response(payload, result) if args.hook else result))
    except Exception as exc:
        if args.hook:
            print(json.dumps({'suppressOutput': True}))
            print('Trace viewer skipped: ' + type(exc).__name__, file=sys.stderr)
        else:
            parser.exit(1, 'Trajectory unavailable: ' + type(exc).__name__ + '\n')


if __name__ == '__main__':
    main()
