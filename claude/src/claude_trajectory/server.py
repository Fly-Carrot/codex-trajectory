"""Shared HTTP and reference helpers for the loopback viewer. No session writes."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import os
from pathlib import Path
import re
import stat as stat_module
from socketserver import TCPServer
from urllib.parse import urlsplit, unquote

try:
    from .events import redact
except ImportError:
    from events import redact


WINDOW = 2 * 1024 * 1024
MAX_LINE = 1024 * 1024
REPORT_SUFFIXES = {'.md', '.html', '.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.wav'}
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


def final_references(text):
    text = re.sub(r'```.*?```|~~~.*?~~~|`[^`]*`', '', text if isinstance(text, str) else '', flags=re.S)
    refs = []
    for match in re.finditer(r'(?<!!)\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]+)\)', text):
        raw = match[1].strip().removeprefix('<').removesuffix('>')
        try:
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or parsed.query:
                continue
            path = unquote(re.sub(r':\d+(?::\d+)?$', '', parsed.path))
            if path and path not in refs:
                refs.append(path)
        except ValueError:
            continue
        if len(refs) >= 16:
            break
    return refs


def verified_reference(cwd, raw):
    if not cwd or not cwd.is_absolute():
        return None
    path = Path(raw)
    path = path if path.is_absolute() else cwd / path
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return None
    if '..' in path.parts or path.suffix.lower() not in REPORT_SUFFIXES:
        return None
    # Walk from the filesystem root with no-follow directory handles. Never read
    # deliverable contents or follow a swapped parent symlink outside the workspace.
    fd = None
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if not stat_module.S_ISREG(info.st_mode):
            return None
        return {'id': hashlib.sha256(str(path).encode()).hexdigest()[:20],
                'name': redact(path.name), 'path': redact(str(relative)), 'size': info.st_size,
                'evidence': 'Referenced in final reply; file exists at refresh. Creation by this task is not verified.'}
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Numeric loopback endpoints need no potentially blocking reverse DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Request lines contain no secrets, but nothing is logged at all.
        pass

    def reply(self, status, body, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)
