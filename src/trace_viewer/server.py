"""Read-only, bounded viewer for one Codex rollout. No inference or session writes."""

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import time
from urllib.parse import urlsplit

try:
    from .events import normalize, public_text, redact, merge_event
except ImportError:
    from events import normalize, public_text, redact, merge_event


WINDOW = 2 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_EVENTS = 160


class RolloutReader:
    def __init__(self, path, thread_id):
        self.path = Path(path).resolve(strict=True)
        self.thread_id = thread_id
        self.lock = threading.Lock()
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.dropping = False
        self.events = OrderedDict()
        self.skipped = 0
        self.version = 0
        self.tail_limited = False
        self.validate()

    def validate(self):
        with self.open_source() as stream:
            self.validate_stream(stream)

    def validate_stream(self, stream):
        stream.seek(0)
        header = json.loads(stream.readline(8 * MAX_LINE))
        if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != self.thread_id:
            raise ValueError("Rollout does not belong to the selected thread")

    def open_source(self):
        # Refuse a file replaced with a symlink after registration.
        return os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW), "rb")

    def snapshot(self):
        with self.lock:
            with self.open_source() as stream:
                stat = os.fstat(stream.fileno())
                self.validate_stream(stream)
                identity = (stat.st_dev, stat.st_ino)
                if self.identity != identity or stat.st_size < self.offset:
                    self.identity = identity
                    self.events.clear()
                    self.dropping = False
                    self.skipped = 0
                    self.offset = max(0, stat.st_size - WINDOW)
                    self.tail_limited = self.offset > 0
                    if self.offset:
                        stream.seek(self.offset)
                        fragment = stream.readline(MAX_LINE)
                        self.dropping = not fragment.endswith(b'\n')
                        self.offset = stream.tell()
                stream.seek(self.offset)
                chunk = stream.read(WINDOW)
                self.validate_stream(stream)
            start = self.offset
            self.offset += len(chunk)
            lines = chunk.split(b"\n")
            partial = lines.pop()
            for line in lines:
                location = start
                start += len(line) + 1
                if self.dropping:
                    self.dropping = False
                    continue
                if len(line) > MAX_LINE:
                    self.skipped += 1
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and isinstance(record.get('payload'), dict):
                        owner = record['payload'].get('thread_id')
                        if owner and owner != self.thread_id:
                            continue
                    event = normalize(record, location) if isinstance(record, dict) else None
                    if event:
                        key = event['id']
                        self.events[key] = merge_event(self.events[key], event) if key in self.events else event
                        while len(self.events) > MAX_EVENTS:
                            self.events.popitem(last=False)
                except (ValueError, TypeError, AttributeError):
                    self.skipped += 1
            if len(partial) > MAX_LINE:
                self.dropping = True
                self.skipped += 1
            elif partial and not self.dropping:
                # Re-read an incomplete line from disk next time. Do not retain
                # raw reasoning, context or tool arguments in an idle buffer.
                self.offset -= len(partial)
            if chunk:
                self.version += 1
            return {"thread_id": self.thread_id, "source": self.path.name,
                    "source_updated": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "version": self.version, "events": sorted(self.events.values(), key=lambda e: e['time']), "skipped": self.skipped,
                    "tail_limited": self.tail_limited, "limit": MAX_EVENTS,
                    "coverage": "Selected rollout only. Recent public events; structured completions appear when recorded. No internal reasoning or model-call timing. Unknown schemas omitted."}


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reader, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.reader = reader
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.last_access = time.monotonic()
        self.html = Path(__file__).with_name("index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host or self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.reply(403, b"Forbidden origin or host")
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.reply(200, self.server.html, "text/html; charset=utf-8")
        elif path == "/api/events":
            auth = self.headers.get("Authorization", "")
            if not secrets.compare_digest(auth.encode(), ("Bearer " + self.server.token).encode()):
                self.reply(401, b"Authentication required")
                return
            self.server.last_access = time.monotonic()
            try:
                data = self.server.reader.snapshot()
                self.reply(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except (OSError, ValueError):
                self.reply(503, b"Selected log unavailable; no other conversation was substituted")
        else:
            self.reply(404, b"Not found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--connection-file", required=True)
    args = parser.parse_args()
    server = ViewerServer(RolloutReader(args.log, args.thread_id))
    connection = Path(args.connection_file)
    data = json.dumps({"url": server.origin + "/#" + server.token, "pid": os.getpid(),
                       "thread_id": args.thread_id, "log": str(server.reader.path)})
    fd = os.open(connection, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    print(f"TRACE_VIEWER_READY port={server.server_port} pid={os.getpid()}", flush=True)

    def idle_stop():
        while True:
            time.sleep(30)
            if time.monotonic() - server.last_access > 3600:
                server.shutdown()
                return

    threading.Thread(target=idle_stop, daemon=True).start()
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # A later rollout for this thread may already own a new connection file.
        try:
            if connection.read_text() == data:
                connection.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
