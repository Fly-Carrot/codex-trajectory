"""One private loopback observer; independent authorization and lazy cache per session.

Adapted from codex-trajectory (MIT, Copyright (c) 2026 Fly-Carrot).
"""

import argparse
import errno
from collections import OrderedDict
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs

if not __package__:
    # Isolated direct entry: import only the installed sibling modules.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from .server import Handler as BaseHandler, LoopbackHTTPServer
    from .history import HistoryReader, decode_cursor
    from .sources import Source, SourcePending, logical_path, check_id, projects_root, read_release
except ImportError:
    from server import Handler as BaseHandler, LoopbackHTTPServer
    from history import HistoryReader, decode_cursor
    from sources import Source, SourcePending, logical_path, check_id, projects_root, read_release

SESSION = r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}'
MAX_READERS = 8
CACHE_TTL = 120
DISK_BUDGET = 512 * 1024 * 1024
INDEX_GROWTH = 16 * 1024 * 1024
SERVICE_SCHEMA = 1
SERVICE_NAME = 'claude-trajectory'
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit', 'Manual')


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('State directory must be private, owned by you, and not a symlink')
    return path


def read_private(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe private state file')
        raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('State file too large')
        return json.loads(raw)


def write_private(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symlinked state file')
    encoded = json.dumps(value).encode()
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError('State file too large; previous state preserved')
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(encoded.decode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def resolve_session(payload, projects):
    """Exact session from a hook payload. A not-yet-written transcript is allowed."""
    if payload.get('hook_event_name') not in HOOK_EVENTS:
        raise ValueError('Unsupported hook event')
    session = check_id(str(payload.get('session_id', '')))
    raw = payload.get('transcript_path')
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or '\0' in raw:
        raise ValueError('Exact transcript path required')
    projects = projects_root(projects)
    log = logical_path(Path(raw).resolve(strict=False), projects, session)
    try:
        with Source(log, session):
            pass
    except (FileNotFoundError, SourcePending):
        # Claude Code creates the file after SessionStart and the first prompt hook.
        pass
    return session, log


def log_state(paths):
    # Status checks stat only; they never read conversation content.
    for path in paths:
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                return 'available'
        except OSError:
            continue
    # The desktop app leaves a small release marker when it deletes a session.
    for path in paths:
        release = read_release(path)
        if release and release['reason'] == 'delete':
            return 'deleted'
    return 'not_found'


class Registry:
    def __init__(self, state, projects, clock=time.monotonic):
        self.state = private_directory(state).resolve()
        self.projects = projects_root(projects)
        self.clock = clock
        self.lock = threading.RLock()
        self.readers = OrderedDict()
        self.thread_locks = {}
        self.busy = {}
        self.reservations = {}
        self.epoch = secrets.token_hex(8)
        try:
            value = read_private(self.state / 'registry.json')
            if value.get('projects_root') != str(self.projects) or value.get('schema') != SERVICE_SCHEMA:
                raise ValueError('Registry belongs to a different projects directory or version')
            self.entries = value['sessions']
            if not isinstance(self.entries, dict):
                raise ValueError('Malformed registry')
        except FileNotFoundError:
            self.entries = {}
        # A stopped loopback port can be reused by another process. Old browser
        # links must not become credentials for a new observer instance.
        if self.entries:
            self.entries = {key: {**entry, 'token': secrets.token_urlsafe(32)} for key, entry in self.entries.items()}
            self.save(self.entries)

    def save(self, entries):
        write_private(self.state / 'registry.json', {'schema': SERVICE_SCHEMA,
                      'projects_root': str(self.projects), 'sessions': entries})

    def register(self, payload):
        session, log = resolve_session(payload, self.projects)
        with self.lock:
            old = self.entries.get(session)
            paths = list(old.get('logs', [old['log']])) if old else []
            if str(log) not in paths:
                paths.append(str(log))
            entry = {'log': str(log), 'logs': sorted(paths), 'token': old['token'] if old else secrets.token_urlsafe(32)}
            changed = old != entry
            if changed:
                updated = {**self.entries, session: entry}
                self.save(updated)
                self.entries = updated
                self.readers.pop(session, None)
            return session, dict(entry), changed

    def authorized(self, session, token):
        with self.lock:
            entry = self.entries.get(session)
            return entry is not None and secrets.compare_digest(token.encode(), ('Bearer ' + entry['token']).encode())

    def evict(self):
        with self.lock:
            now = self.clock()
            for session, (_, _, touched) in list(self.readers.items()):
                if now - touched >= CACHE_TTL and not self.busy.get(session):
                    self.readers.pop(session)
            self.prune_indexes(DISK_BUDGET)

    def cache_files(self):
        files = {}
        for session in self.entries:
            if not re.fullmatch(SESSION, session):
                continue
            directory = self.state / session
            if directory.is_symlink() or directory.resolve() != directory:
                continue
            path = directory / 'history.sqlite3'
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                files[session] = (path, info.st_size, info.st_mtime)
        return files

    def prune_indexes(self, target):
        # Called under the registry lock. Never delete a live/in-flight reader,
        # raw transcript, connection file, or registration, only rebuildable DBs.
        files = self.cache_files()
        total = sum(item[1] for item in files.values())
        for session, (path, size, _) in sorted(files.items(), key=lambda item: item[1][2]):
            if total <= target:
                break
            if session in self.readers or self.busy.get(session):
                continue
            sidecars = [Path(str(path) + suffix) for suffix in ('-journal', '-wal', '-shm')]
            if any(p.is_symlink() for p in sidecars):
                continue
            try:
                path.unlink()
                for extra in sidecars:
                    extra.unlink(missing_ok=True)
                total -= size
            except OSError:
                continue
        return total

    def reserve_index(self, session, growth=INDEX_GROWTH):
        self.prune_indexes(max(0, DISK_BUDGET - growth))
        files = self.cache_files()
        used = sum(item[1] for item in files.values())
        reserved = sum(max(0, limit - files.get(key, (None, 0, 0))[1])
                       for key, limit in self.reservations.items())
        current = files.get(session, (None, 0, 0))[1]
        limit = current + min(growth, max(0, DISK_BUDGET - used - reserved))
        self.reservations[session] = limit
        return limit

    def snapshot(self, session, before=None, detail=None, anchor=None):
        with self.lock:
            self.evict()
            # Reclaim before pinning the requested session, so reopening a formerly
            # full, idle index can rebuild it instead of staying permanently full.
            self.prune_indexes(max(0, DISK_BUDGET - INDEX_GROWTH))
            entry = self.entries[session]
            # Re-check containment on every read, including replaced parent directories.
            for path in entry.get('logs', [entry['log']]):
                logical_path(Path(path), self.projects, session)
            if session not in self.readers:
                while len(self.readers) >= MAX_READERS:
                    self.readers.popitem(last=False)
                directory = private_directory(self.state / session)
                reader = HistoryReader(directory, self.projects, session, entry.get('logs', [entry['log']]))
                reader.lock = self.thread_locks.setdefault(session, threading.RLock())
                self.readers[session] = (reader, secrets.token_hex(8), self.clock())
            reader, generation, _ = self.readers[session]
            self.readers[session] = (reader, generation, self.clock())
            self.readers.move_to_end(session)
            self.busy[session] = self.busy.get(session, 0) + 1
        # File IO and indexing must not hold the cross-session registry lock.
        try:
            with reader.lock:
                with self.lock:
                    growth = INDEX_GROWTH
                    if reader.capacity_limit_hit is not None:
                        growth = max(growth, reader.capacity_limit_hit - reader.path.stat().st_size + INDEX_GROWTH)
                    reader.disk_limit_bytes = self.reserve_index(session, growth)
                try:
                    result = reader.snapshot(before=before, detail=detail, anchor=anchor)
                finally:
                    with self.lock:
                        self.reservations.pop(session, None)
                # Persist LRU usage without copying conversation contents.
                if not reader.path.is_symlink():
                    os.utime(reader.path, None)
        finally:
            with self.lock:
                self.busy[session] -= 1
                if not self.busy[session]:
                    del self.busy[session]
        if 'version' in result:
            result['version'] = self.epoch + ':' + generation + ':' + str(result['version'])
        return result


class BrokerServer(LoopbackHTTPServer):
    daemon_threads = True

    def __init__(self, registry, port=0):
        self.registry = registry
        self.admin_token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.last_access = time.monotonic()
        self.html = Path(__file__).with_name('index.html').read_bytes()
        super().__init__(('127.0.0.1', port), BrokerHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'

    def session_connection(self, session, entry):
        return {'url': self.origin + '/s/' + session + '#' + entry['token'],
                'pid': os.getpid(), 'session_id': session, 'log': entry['log'],
                'instance': self.instance}


class BrokerHandler(BaseHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def allowed_origin(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or self.headers.get('Origin', self.server.origin) != self.server.origin:
            self.reply(403, b'Forbidden origin or host')
            return False
        return True

    def admin(self):
        if not secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + self.server.admin_token).encode()):
            self.reply(401, b'Authentication required')
            return False
        return True

    def json_reply(self, data, status=200):
        self.reply(status, json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        if not self.allowed_origin():
            return
        path = urlsplit(self.path).path
        if path == '/api/identity':
            nonce = parse_qs(urlsplit(self.path).query).get('nonce', [''])[0]
            if not re.fullmatch(r'[a-f0-9]{64}', nonce):
                self.reply(400, b'Invalid challenge')
                return
            proof = hmac.new(self.server.admin_token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            self.json_reply({'proof': proof, 'instance': self.server.instance})
            return
        if path == '/' or re.fullmatch('/s/' + SESSION, path):
            self.reply(200, self.server.html, 'text/html; charset=utf-8')
            return
        if path == '/api/health':
            if self.admin():
                self.json_reply({'service': SERVICE_NAME, 'schema': SERVICE_SCHEMA, 'pid': os.getpid(),
                                 'instance': self.server.instance, 'projects_root': str(self.server.registry.projects)})
            return
        match = re.fullmatch('/api/sessions/(' + SESSION + ')/(events|status)', path)
        if not match:
            self.reply(404, b'Not found')
            return
        session, action = match.groups()
        if not self.server.registry.authorized(session, self.headers.get('Authorization', '')):
            self.reply(401, b'Authentication required')
            return
        self.server.last_access = time.monotonic()
        entry = self.server.registry.entries[session]
        if action == 'status':
            # Link verification must not instantiate a reader or scan conversation content.
            self.json_reply({'session_id': session, 'instance': self.server.instance, 'pid': os.getpid(),
                             'log': entry['log'], 'log_state': log_state(entry.get('logs', [entry['log']]))})
            return
        try:
            query = parse_qs(urlsplit(self.path).query)
            before = query.get('before', [None])[0]
            detail = query.get('detail', [None])[0]
            anchor = query.get('anchor', [None])[0]
            if before:
                decode_cursor(before)
            if detail and len(detail) > 1024:
                raise ValueError('Invalid detail ID')
            if anchor and len(anchor) > 1024:
                raise ValueError('Invalid anchor ID')
        except ValueError:
            self.reply(400, b'Invalid history request')
            return
        try:
            self.json_reply(self.server.registry.snapshot(session, before=before, detail=detail, anchor=anchor))
        except (OSError, ValueError, KeyError, sqlite3.Error):
            # No other session is ever substituted for an unavailable one.
            self.json_reply({'error': 'unavailable', 'message': 'Selected transcript unavailable; no other session was substituted'}, 503)

    def do_POST(self):
        if not self.allowed_origin() or not self.admin():
            return
        if urlsplit(self.path).path != '/api/register':
            self.reply(404, b'Not found')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Invalid body size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid payload')
            session, entry, changed = self.server.registry.register(payload)
        except (OSError, ValueError, TypeError, KeyError):
            self.reply(400, b'Invalid exact-session registration')
            return
        self.server.last_access = time.monotonic()
        self.json_reply({**self.server.session_connection(session, entry), 'registered': changed})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--projects', required=True, type=Path)
    args = parser.parse_args()
    state = private_directory(args.state_dir)
    lock_fd = os.open(state / 'server.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            port = read_private(state / 'endpoint.json')['port']
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError('Invalid saved port')
        except FileNotFoundError:
            port = 0
        registry = Registry(state, args.projects)
        try:
            server = BrokerServer(registry, port)
        except OSError as exc:
            if not port or exc.errno != errno.EADDRINUSE:
                raise
            # Never stop or send credentials to the occupant of a saved port.
            server = BrokerServer(registry, 0)
        write_private(state / 'endpoint.json', {'port': server.server_port})
        connection = state / 'service.json'
        data = {'origin': server.origin, 'pid': os.getpid(), 'token': server.admin_token,
                'instance': server.instance, 'schema': SERVICE_SCHEMA}
        write_private(connection, data)
        # Refresh every registered session, not only the one that started the service.
        for session, entry in server.registry.entries.items():
            directory = private_directory(state / session)
            write_private(directory / 'connection.json', server.session_connection(session, entry))
        stopped = threading.Event()

        def cleanup_idle():
            while not stopped.wait(15):
                server.registry.evict()

        threading.Thread(target=cleanup_idle, daemon=True).start()

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            server.server_close()
            try:
                if read_private(connection) == data:
                    connection.unlink()
            except FileNotFoundError:
                pass


if __name__ == '__main__':
    main()
