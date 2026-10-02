"""One private loopback observer; independent authorization and lazy cache per chat."""

import argparse
from collections import OrderedDict
import fcntl
import hashlib
import hmac
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs

try:
    from .server import Handler as BaseHandler, RolloutReader
except ImportError:
    from server import Handler as BaseHandler, RolloutReader

THREAD = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}')
MAX_READERS = 8
CACHE_TTL = 120
SERVICE_SCHEMA = 1


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


def resolve_session(payload, sessions):
    if payload.get('hook_event_name') not in ('Stop', 'UserPromptSubmit'):
        raise ValueError('Unsupported hook event')
    thread = str(payload.get('session_id', ''))
    if not THREAD.fullmatch(thread):
        raise ValueError('Invalid thread identity')
    log = Path(payload.get('transcript_path') or '').resolve(strict=True)
    if not log.is_file() or not log.is_relative_to(Path(sessions).resolve(strict=True)):
        raise ValueError('Transcript is outside the sessions directory')
    with os.fdopen(os.open(log, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        meta = json.loads(stream.readline(8 * 1024 * 1024))
    data = meta.get('payload', {})
    if meta.get('type') != 'session_meta' or data.get('id') != thread:
        raise ValueError('Transcript does not match the thread')
    if data.get('thread_source') not in (None, 'user') or isinstance(data.get('source'), dict) or data.get('source') in ('exec', 'subagent'):
        raise ValueError('Only main user conversations can be registered')
    return thread, log


class Registry:
    def __init__(self, state, sessions, clock=time.monotonic):
        self.state = private_directory(state)
        self.sessions = Path(sessions).resolve(strict=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.readers = OrderedDict()
        self.epoch = secrets.token_hex(8)
        try:
            value = read_private(self.state / 'registry.json')
            if value.get('sessions_root') != str(self.sessions) or value.get('schema') != SERVICE_SCHEMA:
                raise ValueError('Registry belongs to a different sessions directory or version')
            self.entries = value['threads']
            if not isinstance(self.entries, dict):
                raise ValueError('Malformed registry')
        except FileNotFoundError:
            self.entries = {}
        # A stopped loopback port can be reused by another process. Old browser
        # links must not become credentials for a new observer instance.
        if self.entries:
            self.entries = {key: {**entry, 'token': secrets.token_urlsafe(32)} for key, entry in self.entries.items()}
            write_private(self.state/'registry.json', {'schema': SERVICE_SCHEMA,
                          'sessions_root': str(self.sessions), 'threads': self.entries})

    def register(self, payload):
        thread, log = resolve_session(payload, self.sessions)
        with self.lock:
            old = self.entries.get(thread)
            entry = {'log': str(log), 'token': old['token'] if old else secrets.token_urlsafe(32)}
            changed = old != entry
            if changed:
                updated = {**self.entries, thread: entry}
                write_private(self.state / 'registry.json', {'schema': SERVICE_SCHEMA,
                              'sessions_root': str(self.sessions), 'threads': updated})
                self.entries = updated
                self.readers.pop(thread, None)
            return thread, dict(entry), changed

    def authorized(self, thread, token):
        with self.lock:
            entry = self.entries.get(thread)
            return entry is not None and secrets.compare_digest(token.encode(), ('Bearer ' + entry['token']).encode())

    def evict(self):
        with self.lock:
            now = self.clock()
            for thread, (_, _, touched) in list(self.readers.items()):
                if now - touched >= CACHE_TTL:
                    self.readers.pop(thread)

    def snapshot(self, thread):
        with self.lock:
            self.evict()
            entry = self.entries[thread]
            # Re-check containment on every read, including replaced parent directories.
            path = Path(entry['log'])
            if path.resolve(strict=True) != path or not path.is_relative_to(self.sessions):
                raise ValueError('Registered source path changed')
            if thread not in self.readers:
                while len(self.readers) >= MAX_READERS:
                    self.readers.popitem(last=False)
                self.readers[thread] = (RolloutReader(path, thread), secrets.token_hex(8), self.clock())
            reader, generation, _ = self.readers[thread]
            self.readers[thread] = (reader, generation, self.clock())
            self.readers.move_to_end(thread)
            result = reader.snapshot()
            result['version'] = self.epoch + ':' + generation + ':' + str(result['version'])
            return result


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, registry, port=0):
        self.registry = registry
        self.admin_token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.last_access = time.monotonic()
        self.html = Path(__file__).with_name('index.html').read_bytes()
        super().__init__(('127.0.0.1', port), BrokerHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'

    def chat_connection(self, thread, entry):
        return {'url': self.origin + '/t/' + thread + '#' + entry['token'],
                'pid': os.getpid(), 'thread_id': thread, 'log': entry['log'],
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

    def json_reply(self, data):
        self.reply(200, json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

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
        if path == '/' or re.fullmatch(r'/t/([a-zA-Z0-9][a-zA-Z0-9_-]{0,100})', path):
            self.reply(200, self.server.html, 'text/html; charset=utf-8')
            return
        if path == '/api/health':
            if self.admin():
                self.json_reply({'service': 'trajectory', 'schema': SERVICE_SCHEMA, 'pid': os.getpid(),
                                 'instance': self.server.instance, 'sessions_root': str(self.server.registry.sessions)})
            return
        match = re.fullmatch(r'/api/threads/([a-zA-Z0-9][a-zA-Z0-9_-]{0,100})/(events|status)', path)
        if not match:
            self.reply(404, b'Not found')
            return
        thread, action = match.groups()
        if not self.server.registry.authorized(thread, self.headers.get('Authorization', '')):
            self.reply(401, b'Authentication required')
            return
        self.server.last_access = time.monotonic()
        if action == 'status':
            # Link verification must not instantiate a reader or scan conversation content.
            entry = self.server.registry.entries[thread]
            self.json_reply({'thread_id': thread, 'instance': self.server.instance, 'pid': os.getpid(), 'log': entry['log']})
            return
        try:
            self.json_reply(self.server.registry.snapshot(thread))
        except (OSError, ValueError, KeyError):
            self.reply(503, b'Selected log unavailable; no other conversation was substituted')

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
            thread, entry, changed = self.server.registry.register(payload)
        except (OSError, ValueError, TypeError, KeyError):
            self.reply(400, b'Invalid exact-session registration')
            return
        self.server.last_access = time.monotonic()
        self.json_reply({**self.server.chat_connection(thread, entry), 'registered': changed})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--sessions', required=True, type=Path)
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
        server = BrokerServer(Registry(state, args.sessions), port)
        write_private(state / 'endpoint.json', {'port': server.server_port})
        connection = state / 'service.json'
        data = {'origin': server.origin, 'pid': os.getpid(), 'token': server.admin_token,
                'instance': server.instance, 'schema': SERVICE_SCHEMA}
        write_private(connection, data)
        stopped = threading.Event()

        def cleanup_idle():
            while not stopped.wait(15):
                server.registry.evict()
                if time.monotonic() - server.last_access > 3600:
                    server.shutdown()
                    return

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
