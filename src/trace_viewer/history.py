"""Rebuildable public-event index. Source transcripts are always read-only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    from .events import normalize, merge_event
    from .server import MAX_LINE, WINDOW, final_references, verified_reference, open_regular_source
except ImportError:
    from events import normalize, merge_event
    from server import MAX_LINE, WINDOW, final_references, verified_reference, open_regular_source

PAGE_EVENTS = 300
PAGE_TURNS = 20


def record_order(record):
    stamp = record.get('timestamp', '')
    try:
        value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else ''
    except (ValueError, AttributeError, TypeError):
        return ''


def encode_cursor(stamp, key):
    return base64.urlsafe_b64encode(json.dumps([stamp, key]).encode()).decode()


def decode_cursor(value):
    if not value or len(value) > 2048:
        raise ValueError('Invalid history cursor')
    try:
        pair = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
    except (ValueError, UnicodeError):
        raise ValueError('Invalid history cursor') from None
    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
        raise ValueError('Invalid history cursor')
    return pair


def event_key(record, offset):
    event = normalize(record, offset)
    if event and event['id'].startswith('offset:'):
        # Exact replay deduplication only: never identify messages by their text.
        event['id'] = 'record:' + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    return event


class HistoryReader:
    def __init__(self, directory, sessions, thread, paths):
        self.directory, self.sessions, self.thread = Path(directory), Path(sessions), thread
        self.paths = tuple(paths)
        self.lock = threading.RLock()
        self.path = self.directory / 'history.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Unsafe history index')
        finally:
            os.close(fd)
        self.initialized = False

    def initialize(self):
        if self.initialized:
            return
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, identity TEXT,
                    start INTEGER, offset INTEGER, turn TEXT, skipped INTEGER DEFAULT 0,
                    dropping INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, stamp TEXT, turn TEXT,
                    summary TEXT, refs TEXT);
                CREATE INDEX IF NOT EXISTS event_order ON events(stamp,id);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER);
                INSERT OR IGNORE INTO metadata VALUES('version',0);
            ''')
            if 'dropping' not in {row['name'] for row in db.execute('PRAGMA table_info(sources)')}:
                db.execute('ALTER TABLE sources ADD COLUMN dropping INTEGER DEFAULT 0')
            schema = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
            if not schema or schema[0] != 2:
                # Old summaries were merged in read order. Rebuild this disposable
                # cache once; source transcripts are never modified.
                db.execute('DELETE FROM events')
                db.execute('DELETE FROM sources')
                db.execute("INSERT OR REPLACE INTO metadata VALUES('format',2)")
        self.initialized = True

    @contextmanager
    def connect(self):
        if self.path.is_symlink():
            raise ValueError('Unsafe history index')
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA mmap_size=0')
            with db:
                yield db
        finally:
            db.close()

    def open_source(self, path):
        path = Path(path)
        if path.resolve(strict=True) != path or not path.is_relative_to(self.sessions):
            raise ValueError('Source path changed')
        stream = open_regular_source(path)
        try:
            header = json.loads(stream.readline(8 * MAX_LINE))
            if not isinstance(header, dict) or not isinstance(header.get('payload'), dict):
                raise ValueError('Invalid source header')
            meta = header.get('payload', {})
            if header.get('type') != 'session_meta' or meta.get('id') != self.thread:
                raise ValueError('Wrong source thread')
            if meta.get('thread_source') not in (None, 'user') or isinstance(meta.get('source'), dict) or meta.get('source') in ('exec', 'subagent'):
                raise ValueError('Not a main chat')
            return stream, meta
        except Exception:
            stream.close()
            raise

    def ingest(self, db, path, backward=False):
        stream, _ = self.open_source(path)
        with stream:
            info = os.fstat(stream.fileno())
            identity = f'{info.st_dev}:{info.st_ino}'
            old = db.execute('SELECT * FROM sources WHERE path=?', (path,)).fetchone()
            if old and (old['identity'] != identity or info.st_size < old['offset']):
                raise ValueError('Source replaced or truncated; history index needs explicit rebuild')
            if old and not backward and old['offset'] == info.st_size:
                return False
            if old and backward and old['start'] == 0:
                return False
            end = old['start'] if old and backward else info.st_size
            start = max(0, end - WINDOW) if backward or not old else old['offset']
            stream.seek(start)
            if start and (backward or not old):
                # Align at a complete source record; never parse a partial JSON line.
                while stream.tell() < end:
                    part = stream.readline(min(MAX_LINE + 1, end - stream.tell()))
                    if part.endswith(b'\n') or not part:
                        break
            begin = stream.tell()
            turn = old['turn'] if old and not backward else ''
            dropping = bool(old['dropping']) if old and not backward else False
            skipped = 0
            while stream.tell() < end and stream.tell() - begin < WINDOW:
                offset = stream.tell()
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                if dropping:
                    dropping = not raw.endswith(b'\n')
                    continue
                if len(raw) > MAX_LINE:
                    dropping = not raw.endswith(b'\n')
                    skipped += 1
                    continue
                if not raw.endswith(b'\n'):
                    stream.seek(offset)
                    break
                try:
                    record = json.loads(raw)
                    payload = record.get('payload', {})
                    if not isinstance(payload, dict):
                        continue
                    if payload.get('thread_id') not in (None, self.thread):
                        continue
                    if record.get('type') == 'turn_context' or payload.get('type') == 'task_started':
                        turn = str(payload.get('turn_id') or '')
                    event = event_key(record, offset)
                    if not event:
                        continue
                    event['turn_id'] = event['turn_id'] or turn
                    ref = [path, offset, len(raw), hashlib.sha256(raw).hexdigest()]
                    previous = db.execute('SELECT summary,refs FROM events WHERE id=?', (event['id'],)).fetchone()
                    refs = json.loads(previous['refs']) if previous else []
                    if not any(r[0:2] == ref[0:2] for r in refs):
                        refs.append(ref)
                    # Keep a first input and recent structured/result records.
                    refs = refs[:1] + refs[-7:] if len(refs) > 8 else refs
                    if previous:
                        prior = json.loads(previous['summary'])
                        if record_order(record) < prior.get('_record_order', ''):
                            event = merge_event(event, prior)
                        else:
                            event = merge_event(prior, event)
                        event['_record_order'] = max(record_order(record), prior.get('_record_order', ''))
                    else:
                        event['_record_order'] = record_order(record)
                    summary = dict(event)
                    for field in ('detail', 'input', 'output'):
                        summary[field] = str(summary.get(field, ''))[:180]
                    summary['detail_available'] = True
                    db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)',
                               (event['id'], event['time'], event['turn_id'], json.dumps(summary), json.dumps(refs)))
                except (ValueError, TypeError, AttributeError):
                    skipped += 1
            finish = stream.tell()
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)',
                       (path, identity, (start if begin >= end else begin) if not old or backward else old['start'],
                        old['offset'] if old and backward else finish,
                        old['turn'] if old and backward else turn, (old['skipped'] if old else 0) + skipped,
                        old['dropping'] if old and backward else int(dropping)))
            if finish != begin:
                db.execute("UPDATE metadata SET value=value+1 WHERE key='version'")
            return True

    def full_event(self, db, key, reference_budget=16):
        row = db.execute('SELECT refs FROM events WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError('Event is no longer indexed')
        event, artifacts, records = None, [], []
        for path, offset, length, digest in json.loads(row['refs']):
            if path not in self.paths:
                continue
            stream, meta = self.open_source(path)
            with stream:
                stream.seek(offset)
                raw = stream.read(min(length, MAX_LINE))
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Indexed source record changed')
            record = json.loads(raw)
            incoming = event_key(record, offset)
            if not incoming or incoming['id'] != key:
                raise ValueError('Indexed event identity changed')
            records.append((record_order(record), offset, incoming, record, meta))
        for _, _, incoming, record, meta in sorted(records, key=lambda entry: entry[:2]):
            event = merge_event(event, incoming) if event else incoming
            if incoming.get('reply_phase') == 'Final':
                cwd = meta.get('cwd')
                references = final_references(record)[:reference_budget]
                reference_budget -= len(references)
                artifacts = [a for ref in references
                             if (a := verified_reference(Path(cwd) if isinstance(cwd, str) else None, ref))]
        if event is None:
            raise ValueError('No readable event source')
        if artifacts:
            event['artifacts'] = artifacts
        return event

    def snapshot(self, before=None, detail=None, anchor=None):
        with self.lock:
            self.initialize()
            return self._snapshot(before, detail, anchor)

    def _snapshot(self, before=None, detail=None, anchor=None):
        boundary = decode_cursor(before) if before else None
        with self.connect() as db:
            if detail:
                return {'event': self.full_event(db, detail)}
            if not any(Path(path).exists() for path in self.paths):
                raise FileNotFoundError('All registered source segments are unavailable')
            warnings = []
            changed = False
            # Reserve one of eight chunks for backfill; rotate forward sources fairly.
            position = db.execute("SELECT value FROM metadata WHERE key='source_position'").fetchone()
            position = position[0] if position else 0
            paths = self.paths[::-1]
            count = min(7, len(paths))
            scheduled = [paths[(position+i) % len(paths)] for i in range(count)]
            db.execute("INSERT OR REPLACE INTO metadata VALUES('source_position',?)",
                       ((position+count) % max(1, len(paths)),))
            for path in scheduled:
                try:
                    changed = self.ingest(db, path) or changed
                except FileNotFoundError:
                    warnings.append('A registered segment is unavailable')
            indexed = db.execute('SELECT * FROM sources ORDER BY path').fetchall()
            # Backfill one bounded historical chunk only while a page is open.
            # Paging is marked indexing until all earlier ranges are indexed.
            for source in reversed(indexed):
                if source['start'] > 0:
                    try:
                        changed = self.ingest(db, source['path'], backward=True) or changed
                        break
                    except FileNotFoundError:
                        warnings.append('A registered segment is unavailable')
            indexed = db.execute('SELECT * FROM sources ORDER BY path').fetchall()
            pending = (any(Path(row['path']).exists() and Path(row['path']).stat().st_size > row['offset'] for row in indexed)
                       or any(path not in {row['path'] for row in indexed} and Path(path).exists() for path in self.paths)
                       or any(row['start'] > 0 and Path(row['path']).exists() for row in indexed))
            # Never hand out a cursor over an incomplete index: late backfill could
            # otherwise insert records beyond a cursor already consumed by a client.
            if pending:
                boundary = None
            where, args = ('WHERE (stamp,id)<(?,?)', boundary) if boundary else ('', [])
            if anchor and not pending:
                found = db.execute('SELECT stamp,id FROM events WHERE id=?', (anchor,)).fetchone()
                if found:
                    where, args = 'WHERE (stamp,id)<=(?,?)', [found['stamp'], found['id']]
                else:
                    where, args = '', []
                    warnings.append('Saved event is unavailable; showing latest history')
            rows = db.execute('SELECT * FROM events '+where+' ORDER BY stamp DESC,id DESC LIMIT ?',
                              [*args, PAGE_EVENTS+1]).fetchall()
            selected, turns = [], set()
            for row in rows[:PAGE_EVENTS]:
                if row['turn'] and row['turn'] not in turns and len(turns) >= PAGE_TURNS:
                    break
                if row['turn']:
                    turns.add(row['turn'])
                selected.append(row)
            has_earlier = len(selected) < len(rows) or pending
            events = [json.loads(row['summary']) for row in reversed(selected)]
            for event in events:
                event.pop('_record_order', None)
            # Artifact metadata is verified lazily from final public messages only.
            reference_budget = 32
            for event in reversed(events):
                if event.get('reply_phase') == 'Final' and reference_budget:
                    try:
                        event['artifacts'] = self.full_event(db, event['id'], min(16, reference_budget)).get('artifacts', [])
                    except (OSError, ValueError):
                        event['artifacts'] = []
                    reference_budget -= min(16, reference_budget)
            cursor = encode_cursor(selected[-1]['stamp'], selected[-1]['id']) if selected else before
            updated = max((Path(row['path']).stat().st_mtime for row in indexed if Path(row['path']).exists()), default=0)
            artifact_version = hashlib.sha256(json.dumps([e.get('artifacts', []) for e in events]).encode()).hexdigest()[:12]
            return {'thread_id': self.thread, 'source': 'Registered conversation segments',
                    'source_updated': datetime.fromtimestamp(updated, timezone.utc).isoformat(),
                    'version': str(db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0]) + ':' + str(before) + ':' + str(anchor) + ':' + artifact_version,
                    'events': events, 'limit': PAGE_EVENTS, 'turn_limit': PAGE_TURNS,
                    'next_cursor': cursor if has_earlier and not pending else None, 'has_earlier': has_earlier,
                    'indexing': pending, 'skipped': sum(row['skipped'] for row in indexed),
                    'tail_limited': has_earlier, 'warnings': sorted(set(warnings)),
                    'coverage': 'Recent 20 turns, at most 300 events per page. Load earlier for older history. Source logs are read-only.'}
