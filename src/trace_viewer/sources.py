"""Resolve read-only rollout representations without changing registered identities."""
import json
import os
from pathlib import Path
import stat
import time

HEADER_LIMIT = 8 * 1024 * 1024
DETAIL_SCAN_LIMIT = 16 * 1024 * 1024
INPUT_BUDGET = 16 * 1024 * 1024


class BudgetInput:
    def __init__(self, raw):
        self.raw = raw
        self.reset()

    def reset(self):
        self.remaining = INPUT_BUDGET
        self.deadline = time.monotonic() + 2

    def read(self, size=-1):
        if self.remaining <= 0 or time.monotonic() > self.deadline:
            raise ValueError('Compressed input exceeds the per-refresh safety budget')
        data = self.raw.read(min(self.remaining, 65536 if size < 0 else size))
        self.remaining -= len(data)
        return data

    def tell(self):
        return self.raw.tell()

    def seekable(self):
        return False


class SourcePending(Exception):
    """A bounded forward scan will continue at the next refresh."""


def logical_path(path, sessions):
    path, sessions = Path(path), Path(sessions)
    if str(path).endswith('.jsonl.zst'):
        path = Path(str(path)[:-4])
    archive = sessions.parent / 'archived_sessions'
    if (not path.is_absolute() or path.resolve(strict=False) != path
            or not (path.is_relative_to(sessions) or path.parent == archive)):
        raise ValueError('Source path changed or is outside transcript roots')
    return path


def candidates(path, sessions):
    path = logical_path(path, sessions)
    yield path
    yield Path(str(path) + '.zst')
    archive = Path(sessions).parent / 'archived_sessions' / path.name
    if archive != path:
        yield archive
        yield Path(str(archive) + '.zst')


def open_regular(path):
    # Directory handles prevent parent symlink swaps, not only leaf symlinks.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(leaf).st_mode):
                raise ValueError('Source is not a regular file')
            return os.fdopen(leaf, 'rb')
        except Exception:
            os.close(leaf)
            raise
    finally:
        os.close(fd)


def locate(path, sessions):
    for candidate in candidates(path, sessions):
        try:
            with open_regular(candidate) as stream:
                return candidate, os.fstat(stream.fileno())
        except FileNotFoundError:
            continue
    raise FileNotFoundError('A registered segment is unavailable')


def decoder(raw):
    try:
        from compression import zstd
    except ImportError:
        try:
            from backports import zstd
        except ImportError:
            raise ValueError('Compressed history requires Python 3.14+ or the backports.zstd package') from None
    budget = BudgetInput(raw)
    return CheckedDecoder(zstd.ZstdFile(budget, options={zstd.DecompressionParameter.window_log_max: 23}), zstd.ZstdError, budget)


class CheckedDecoder:
    def __init__(self, stream, error, budget):
        self.stream, self.error, self.budget = stream, error, budget

    def reset_budget(self):
        self.budget.reset()

    def read(self, size):
        try:
            return self.stream.read(size)
        except (EOFError, self.error):
            raise ValueError('Compressed segment is corrupt or incomplete') from None

    def readline(self, size):
        try:
            return self.stream.readline(size)
        except (EOFError, self.error):
            raise ValueError('Compressed segment is corrupt or incomplete') from None

    def tell(self):
        return self.stream.tell()

    def close(self):
        self.stream.close()


def identity(path, info):
    return (f'zstd:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'
            if path.name.endswith('.jsonl.zst') else f'{info.st_dev}:{info.st_ino}')


class Source:
    def __init__(self, path, sessions, thread):
        self.raw = self.stream = None
        self.physical, _ = locate(path, sessions)
        self.raw = open_regular(self.physical)
        self.info = os.fstat(self.raw.fileno())
        self.compressed = self.physical.name.endswith('.jsonl.zst')
        self.identity = identity(self.physical, self.info)
        try:
            self.stream = decoder(self.raw) if self.compressed else self.raw
            header = json.loads(self.stream.readline(HEADER_LIMIT + 1))
            meta = header.get('payload') if isinstance(header, dict) else None
            if (not isinstance(meta, dict) or header.get('type') != 'session_meta'
                    or meta.get('id') != thread):
                raise ValueError('Wrong source thread or invalid source header')
            if (meta.get('thread_source') not in (None, 'user') or isinstance(meta.get('source'), dict)
                    or meta.get('source') in ('exec', 'subagent')):
                raise ValueError('Not a main chat')
            self.meta = meta
        except Exception:
            self.close()
            raise

    def advance(self, offset, budget=DETAIL_SCAN_LIMIT):
        if not self.compressed:
            self.stream.seek(offset)
            return
        if offset < self.stream.tell():
            raise ValueError('Compressed scan must move forward')
        remaining = offset - self.stream.tell()
        for _ in range((min(remaining, budget) + 65535) // 65536):
            data = self.stream.read(min(65536, remaining, budget))
            if not data:
                raise ValueError('Compressed source ended before the indexed offset')
            remaining -= len(data)
            budget -= len(data)
        if remaining:
            raise SourcePending('Compressed detail exceeds the bounded scan; history summaries remain available')

    def close(self):
        if self.stream is not None:
            self.stream.close()
        if self.raw is not None:
            self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
