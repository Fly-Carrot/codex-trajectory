"""Register an exact Claude Code session with one shared local viewer. No default popup.

Adapted from codex-trajectory (MIT, Copyright (c) 2026 Fly-Carrot).
"""
import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

try:
    from .broker import resolve_session, private_directory, read_private, write_private, SERVICE_SCHEMA, SERVICE_NAME
    from .sources import default_projects, check_id, logical_path, PROJECT, projects_root
except ImportError:
    from broker import resolve_session, private_directory, read_private, write_private, SERVICE_SCHEMA, SERVICE_NAME
    from sources import default_projects, check_id, logical_path, PROJECT, projects_root

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / '.claude-trajectory-local/runtime'
ERROR_LOG_LIMIT = 32 * 1024
CONTEXT = ('claude-trajectory verified this exact session. If useful, end your final reply with this single link: '
           '[Trajectory]({url}). Do not open a browser. Keep this private local link out of files, commits and '
           'public text. The observer is optional: never let it block or delay work.')


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
    # Prove the listener holds the admin secret before sending it any credential.
    nonce = secrets.token_hex(32)
    try:
        proof = request(data['origin'], '/api/identity?nonce=' + nonce, '')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(proof, dict):
        return False
    expected = hmac.new(data['token'].encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return proof.get('instance') == data['instance'] and hmac.compare_digest(str(proof.get('proof', '')), expected)


def service_health(state, projects):
    try:
        data = read_private(state / 'service.json')
        if not service_identity(data):
            return None
        actual = request(data['origin'], '/api/health', data['token'])
        if (actual.get('service') != SERVICE_NAME or actual.get('schema') != SERVICE_SCHEMA
                or actual.get('instance') != data.get('instance') or actual.get('pid') != data.get('pid')):
            return None
        if actual.get('projects_root') != str(projects_root(projects)):
            raise ValueError('Running service belongs to another projects directory')
        return data
    except (OSError, KeyError, TypeError):
        return None


def healthy(connection, session, log=None):
    try:
        data = read_private(connection)
        url = urlsplit(data['url'])
        if data.get('session_id') != session or url.path != '/s/' + session or not url.fragment:
            return None
        origin = url._replace(path='', query='', fragment='').geturl()
        service = read_private(Path(connection).parent.parent / 'service.json')
        if service['origin'] != origin or data.get('instance') != service['instance'] or not service_identity(service):
            return None
        actual = request(origin, '/api/sessions/' + session + '/status', url.fragment)
        if actual.get('session_id') != session or actual.get('instance') != service['instance'] or actual.get('pid') != service['pid']:
            return None
        if log is not None and actual.get('log') != str(Path(log)):
            return None
        return {**data, 'instance': actual.get('instance'), 'pid': actual.get('pid', data.get('pid')),
                'log_state': actual.get('log_state')}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def open_browser(url):
    subprocess.run(['/usr/bin/open', '-g', url], check=True, timeout=5,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(payload, state=DEFAULT_STATE, projects=None, auto_open=False, opener=open_browser):
    projects = Path(projects or default_projects())
    session, log = resolve_session(payload, projects)
    state = private_directory(state)
    directory = private_directory(state / session)
    fd = os.open(state / 'launch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return {'status': 'launch_in_progress', 'session_id': session}
                time.sleep(.05)
        service = service_health(state, projects)
        reused = service is not None
        if service is None:
            child = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).with_name('broker.py')),
                                      '--projects', str(projects), '--state-dir', str(state)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('Viewer not started; saved port may be occupied or state invalid. No other process was stopped.')
                service = service_health(state, projects)
                if service:
                    break
                time.sleep(.05)
            if not service:
                child.terminate()
                child.wait(timeout=2)
                raise RuntimeError('Viewer startup timed out')
            threading.Thread(target=child.wait, daemon=True).start()
        data = request(service['origin'], '/api/register', service['token'],
                       {'hook_event_name': payload['hook_event_name'], 'session_id': session, 'transcript_path': str(log)})
        connection = directory / 'connection.json'
        write_private(connection, data)
        verified = healthy(connection, session, log)
        if not verified:
            raise RuntimeError('Registered link did not verify')
        opened = False
        stamp = directory / 'opened.json'
        try:
            previous = read_private(stamp)
        except FileNotFoundError:
            previous = None
        # Optional manual convenience only. Hooks never open a browser.
        if auto_open and previous != {'url': data['url']}:
            opener(data['url'])
            write_private(stamp, {'url': data['url']})
            opened = True
        return {'status': 'reused' if reused else 'started', 'session_id': session, 'opened': opened,
                'connection_file': str(connection), 'pid': data['pid'], 'url': data['url'],
                'log_state': verified.get('log_state')}


def find_transcript(session, projects):
    """Exact filename lookup for an explicit session ID; refuses ambiguity."""
    check_id(session)
    projects = Path(projects).resolve(strict=True)
    matches = [p for p in projects.glob('*/' + session + '.jsonl') if PROJECT.fullmatch(p.parent.name)]
    if len(matches) != 1:
        raise ValueError('Transcript for this session ID not found exactly once; pass --transcript')
    return logical_path(matches[0], projects, session)


def manual_payload(session, transcript, projects):
    if transcript:
        transcript = Path(transcript).expanduser().absolute()
        derived = transcript.name.removesuffix('.jsonl')
        if session and session != derived:
            raise ValueError('Session ID does not match the transcript filename')
        session = derived
    elif session:
        transcript = find_transcript(session, projects)
    else:
        raise ValueError('Pass --session-id or --transcript')
    return {'hook_event_name': 'Manual', 'session_id': check_id(session), 'transcript_path': str(transcript)}


def check_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Unsafe private state directory')


def resume(state, session, projects=None):
    check_id(session)
    state = Path(state)
    projects = projects_root(projects or default_projects())
    check_directory(state)
    registry = read_private(state / 'registry.json')
    if (not isinstance(registry, dict) or registry.get('schema') != SERVICE_SCHEMA
            or registry.get('projects_root') != str(projects)
            or not isinstance(registry.get('sessions'), dict)):
        raise ValueError('Invalid registry')
    entry = registry['sessions'].get(session)
    if (not isinstance(entry, dict) or not isinstance(entry.get('log'), str) or not entry['log']
            or not isinstance(entry.get('logs', []), list)
            or any(not isinstance(path, str) or not path for path in entry.get('logs', []))):
        raise ValueError('No registered transcript for this exact session')
    # Prefer the current source, then only previously registered segments.
    # Persisted paths are canonical; never follow a replacement symlink.
    for source in dict.fromkeys([entry['log'], *reversed(entry.get('logs', []))]):
        path = Path(source)
        try:
            if path.resolve(strict=False) != path:
                raise ValueError('Registered source path changed')
            _, verified = resolve_session({'hook_event_name': 'Manual', 'session_id': session,
                                           'transcript_path': str(path)}, projects)
            if verified != path:
                raise ValueError('Registered source path changed')
        except (OSError, ValueError, TypeError):
            continue
        break
    else:
        raise ValueError('No valid registered transcript path for this exact session')
    result = launch({'hook_event_name': 'Manual', 'session_id': session, 'transcript_path': str(path)},
                    state=state, projects=projects, auto_open=False)
    if not result.get('url'):
        raise RuntimeError('Resume did not produce a verified link; retry after the active launch')
    return result


def current_link(state, session):
    check_id(session)
    data = healthy(Path(state) / session / 'connection.json', session)
    if not data:
        raise RuntimeError('No verified live link for this session. Run resume or register first.')
    return {'status': 'available', 'session_id': session, 'url': data['url'], 'log_state': data.get('log_state')}


def stop(state, projects=None):
    """Stop only the verified observer recorded in this state directory."""
    state = Path(state)
    check_directory(state)
    service = service_health(state, projects or default_projects())
    if not service:
        return {'status': 'not_running'}
    os.kill(service['pid'], signal.SIGTERM)
    return {'status': 'stopped', 'pid': service['pid']}


def hook(payload, state, projects=None):
    """Bounded registration for SessionStart / UserPromptSubmit. Returns hook stdout."""
    event = payload.get('hook_event_name')
    if event not in ('SessionStart', 'UserPromptSubmit'):
        return ''
    result = launch(payload, state=state, projects=projects)
    if not result.get('url'):
        return ''
    marker = Path(state) / result['session_id'] / 'supplied.json'
    if event == 'SessionStart':
        # New, resumed, cleared or compacted context: supply the link again next prompt.
        marker.unlink(missing_ok=True)
        return ''
    try:
        if read_private(marker) == {'url': result['url']}:
            return ''
    except (FileNotFoundError, ValueError):
        pass
    write_private(marker, {'url': result['url']})
    return json.dumps({'suppressOutput': True, 'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit', 'additionalContext': CONTEXT.format(url=result['url'])}})


def record_hook_error(state, event, exc):
    # Bounded, private, and free of payload or transcript content.
    try:
        state = private_directory(state)
        path = state / 'hook-errors.log'
        line = time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + str(event)[:32] + ' ' + type(exc).__name__ + '\n'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if os.fstat(stream.fileno()).st_size + len(line) > ERROR_LOG_LIMIT:
                stream.seek(0)
                tail = stream.read()[-ERROR_LOG_LIMIT // 2:]
                stream.seek(0)
                stream.truncate()
                stream.write(tail[tail.find('\n') + 1:])
            stream.write(line)
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog='claude-trajectory', description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--projects', type=Path, help='Claude Code projects directory (default: ~/.claude/projects)')
    commands = parser.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register', help='Register an exact transcript and print its verified link')
    register.add_argument('--session-id')
    register.add_argument('--transcript', help='Exact <session-id>.jsonl path')
    register.add_argument('--auto-open', action='store_true', help='Open the link once on macOS (never the default)')
    for name, text in (('url', 'Print the verified live link; never starts the service'),
                       ('resume', 'Start the service if needed and print a fresh verified link')):
        sub = commands.add_parser(name, help=text)
        sub.add_argument('--session-id', default=os.environ.get('CLAUDE_CODE_SESSION_ID'),
                         help='Defaults to CLAUDE_CODE_SESSION_ID when run inside Claude Code')
    commands.add_parser('hook', help='Hook entry: read one hook payload on stdin')
    commands.add_parser('stop', help='Stop the verified observer for this state directory')
    args = parser.parse_args(argv)
    if args.command == 'hook':
        event = None
        try:
            payload = json.loads(sys.stdin.read(1024 * 1024))
            event = payload.get('hook_event_name') if isinstance(payload, dict) else None
            if not isinstance(payload, dict):
                raise ValueError('Hook payload must be an object')
            output = hook(payload, args.state_dir, args.projects)
            if output:
                print(output)
        except Exception as exc:
            # Observer failures never block, deny or alter the user's task.
            record_hook_error(args.state_dir, event, exc)
        return
    try:
        if args.command == 'register':
            projects = args.projects or default_projects()
            result = launch(manual_payload(args.session_id, args.transcript, projects),
                            state=args.state_dir, projects=projects, auto_open=args.auto_open)
        elif args.command == 'url':
            if not args.session_id:
                parser.error('url requires --session-id outside Claude Code')
            result = current_link(args.state_dir, args.session_id)
        elif args.command == 'resume':
            if not args.session_id:
                parser.error('resume requires --session-id outside Claude Code')
            result = resume(args.state_dir, args.session_id, args.projects)
        else:
            result = stop(args.state_dir, args.projects)
        print(json.dumps(result))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, 'claude-trajectory: ' + reason(exc) + '\n')


def reason(exc):
    if isinstance(exc, FileNotFoundError):
        return 'not registered yet or state missing (' + type(exc).__name__ + ')'
    if isinstance(exc, ConnectionError):
        return 'service offline (' + type(exc).__name__ + '); run resume'
    message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
    return message[:300]


if __name__ == '__main__':
    main()
