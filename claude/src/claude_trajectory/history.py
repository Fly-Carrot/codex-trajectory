"""Rebuildable public-event index. Source transcripts are always read-only.

Paging, record proofs and capacity handling are adapted from codex-trajectory
(MIT, Copyright (c) 2026 Fly-Carrot).
"""
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    from .events import normalize, merge_event
    from .sources import Source, SourcePending, locate, identity as source_identity, agent_of, read_meta, read_release, subagent_path, ID
    from .server import MAX_LINE, WINDOW, final_references, verified_reference
except ImportError:
    from events import normalize, merge_event
    from sources import Source, SourcePending, locate, identity as source_identity, agent_of, read_meta, read_release, subagent_path, ID
    from server import MAX_LINE, WINDOW, final_references, verified_reference

PAGE_EVENTS = 300
PAGE_TURNS = 20
MAX_SUBAGENTS = 64
FORMAT = 3


class IndexReset(ValueError):
    """An indexed record changed on disk. The disposable index is rebuilt."""


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


class HistoryReader:
    def __init__(self, directory, projects, session, paths):
        self.directory, self.projects, self.session = Path(directory), Path(projects), session
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
        self.validation = {}
        self.notices = set()
        self.disk_limit_bytes = None
        self.capacity_limit_hit = None
        self.capacity_retry_at = 0

    def initialize(self):
        if self.initialized:
            return
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                schema = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
                columns = {row['name'] for row in db.execute('PRAGMA table_info(sources)')}
                if not schema or schema[0] != FORMAT or 'tail_size' not in columns:
                    # Older layouts are rebuilt from scratch; only this disposable cache is dropped.
                    for table in ('events', 'sources', 'record_proofs', 'agents', 'metadata'):
                        db.execute('DROP TABLE IF EXISTS ' + table)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, identity TEXT,
                    start INTEGER, offset INTEGER, turn TEXT, skipped INTEGER DEFAULT 0,
                    dropping INTEGER DEFAULT 0, foreign_records INTEGER DEFAULT 0, tail_size INTEGER DEFAULT -1);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, stamp TEXT, turn TEXT,
                    summary TEXT, refs TEXT);
                CREATE INDEX IF NOT EXISTS event_order ON events(stamp,id);
                CREATE TABLE IF NOT EXISTS record_proofs(path TEXT, offset INTEGER,
                    length INTEGER, digest TEXT, PRIMARY KEY(path,offset));
                CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, call_id TEXT, agent_type TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER);
                INSERT OR IGNORE INTO metadata VALUES('version',0);
            ''')
            schema = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
            if not schema or schema[0] != FORMAT:
                self.clear(db)
                db.execute("INSERT OR REPLACE INTO metadata VALUES('format',?)", (FORMAT,))
        self.initialized = True

    def clear(self, db):
        # Only this rebuildable cache is cleared; transcripts are never modified.
        for table in ('events', 'sources', 'record_proofs', 'agents'):
            db.execute('DELETE FROM ' + table)
        db.execute("UPDATE metadata SET value=value+1 WHERE key='version'")
        self.validation.clear()

    @contextmanager
    def connect(self):
        if self.path.is_symlink():
            raise ValueError('Unsafe history index')
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA mmap_size=0')
            if self.disk_limit_bytes is not None:
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                db.execute(f'PRAGMA max_page_count={max(1, self.disk_limit_bytes // page_size)}')
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        pass

    def context(self, db, path):
        agent = agent_of(path)
        if not agent:
            return {}
        row = db.execute('SELECT agent_type FROM agents WHERE agent_id=?', (agent,)).fetchone()
        return {'agent': agent, 'agent_type': row['agent_type'] if row and row['agent_type'] else ''}

    def open_source(self, path):
        return Source(path, self.session, agent_of(path))

    def subagents(self, db, mains, warnings):
        """Subagent transcripts linked to a dispatch recorded in this exact session."""
        linked = []
        for main in mains:
            directory = Path(main).parent / self.session / 'subagents'
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                names = sorted(entry.name for entry in os.scandir(directory)
                               if entry.name.startswith('agent-') and entry.name.endswith('.jsonl'))
            except OSError:
                continue
            if len(names) > MAX_SUBAGENTS:
                warnings.append(f'More than {MAX_SUBAGENTS} subagent transcripts; later ones are not shown.')
                names = names[:MAX_SUBAGENTS]
            for name in names:
                path = directory / name
                agent = agent_of(path)
                try:
                    subagent_path(path, self.projects, self.session, agent)
                except ValueError:
                    continue
                if not db.execute('SELECT 1 FROM agents WHERE agent_id=?', (agent,)).fetchone():
                    try:
                        meta = read_meta(path.with_name('agent-' + agent + '.meta.json'))
                    except (OSError, ValueError):
                        continue
                    call = meta.get('toolUseId')
                    row = db.execute('SELECT summary FROM events WHERE id=?', ('call:' + str(call),)).fetchone() if isinstance(call, str) else None
                    summary = json.loads(row['summary']) if row else {}
                    # Internal helper agents have no recorded dispatch in this session.
                    if summary.get('category') != 'AGENT' or summary.get('lane') != 'tools':
                        continue
                    # A dispatch that recorded its agent ID links only that agent.
                    if summary.get('agent_id') and summary['agent_id'] != agent:
                        continue
                    kind = meta.get('agentType') if isinstance(meta.get('agentType'), str) and ID.fullmatch(meta['agentType']) else summary.get('agent_type', '')
                    db.execute('INSERT OR REPLACE INTO agents VALUES(?,?,?)', (agent, call, kind))
                linked.append(str(path))
        return linked

    def revalidate(self, db, path, source, old):
        # Device/inode values are not durable identities across mounts, restores or
        # reboots. Accept a new storage identity only after the saved record proofs
        # match again; validate incrementally rather than trust a remap.
        info = source.info
        signature = (source.identity, info.st_size, info.st_mtime_ns)
        prior, cursor = self.validation.get(path, (None, -1))
        if prior != signature:
            cursor = -1
        if info.st_size < old['offset']:
            raise IndexReset('Source truncated')
        rows = db.execute('SELECT offset,length,digest FROM record_proofs WHERE path=? AND offset>? ORDER BY offset LIMIT 4096',
                          (path, cursor)).fetchall()
        consumed = 0
        for proof in rows:
            if not 0 < proof['length'] <= MAX_LINE:
                raise IndexReset('Invalid indexed record length')
            source.stream.seek(proof['offset'])
            raw = source.stream.read(proof['length'])
            if hashlib.sha256(raw).hexdigest() != proof['digest']:
                raise IndexReset('Indexed source record changed')
            cursor = proof['offset']
            consumed += len(raw)
            if consumed >= WINDOW:
                break
        self.validation[path] = (signature, cursor)
        if db.execute('SELECT 1 FROM record_proofs WHERE path=? AND offset>? LIMIT 1', (path, cursor)).fetchone():
            return False
        db.execute('UPDATE sources SET identity=? WHERE path=?', (source.identity, path))
        self.validation.pop(path, None)
        return True

    def ingest(self, db, path, backward=False):
        with self.open_source(path) as source:
            stream, ident, info = source.stream, source.identity, source.info
            context = self.context(db, path)
            old = db.execute('SELECT * FROM sources WHERE path=?', (path,)).fetchone()
            if old and old['identity'] != ident and not self.revalidate(db, path, source, old):
                return False
            if old and info.st_size < old['offset']:
                raise IndexReset('Source truncated')
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
            skipped = foreign = 0
            changed = False
            tail_size = -1
            while stream.tell() < end and stream.tell() - begin < WINDOW:
                offset = stream.tell()
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                digest = hashlib.sha256(raw).hexdigest()
                proof = db.execute('SELECT offset,length,digest FROM record_proofs WHERE path=? AND offset>=? AND offset<? ORDER BY offset LIMIT 1',
                                   (path, offset, stream.tell())).fetchone()
                if proof and (proof['offset'] != offset or proof['length'] != len(raw) or proof['digest'] != digest):
                    raise IndexReset('Indexed source record changed')
                if dropping:
                    dropping = not raw.endswith(b'\n')
                    continue
                if len(raw) > MAX_LINE:
                    dropping = not raw.endswith(b'\n')
                    skipped += 1
                    continue
                if not raw.endswith(b'\n'):
                    # A partially flushed final line is re-read once the file grows.
                    stream.seek(offset)
                    tail_size = info.st_size
                    break
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        skipped += 1
                        continue
                    if record.get('sessionId', self.session) != self.session or (
                            context and record.get('agentId', context['agent']) != context['agent']):
                        foreign += 1
                        continue
                    if record.get('type') == 'user' and isinstance(record.get('promptId'), str):
                        turn = record['promptId'][:128]
                    events = normalize(record, {**context, 'turn': turn})
                except (ValueError, TypeError, AttributeError, RecursionError):
                    skipped += 1
                    continue
                if not events:
                    continue
                ref = [path, offset, len(raw), digest]
                db.execute('INSERT OR IGNORE INTO record_proofs VALUES(?,?,?,?)', ref)
                for event in events:
                    self.store(db, event, ref, record)
                changed = True
            finish = stream.tell()
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?)',
                       (path, ident, (start if begin >= end else begin) if not old or backward else old['start'],
                        old['offset'] if old and backward else finish,
                        old['turn'] if old and backward else turn, (old['skipped'] if old else 0) + skipped,
                        old['dropping'] if old and backward else int(dropping),
                        (old['foreign_records'] if old else 0) + foreign,
                        old['tail_size'] if old and backward else tail_size))
            if finish != begin or changed:
                db.execute("UPDATE metadata SET value=value+1 WHERE key='version'")
            return True

    def store(self, db, event, ref, record):
        previous = db.execute('SELECT summary,refs FROM events WHERE id=?', (event['id'],)).fetchone()
        refs = json.loads(previous['refs']) if previous else []
        if not any(r[0:2] == ref[0:2] for r in refs):
            refs.append(ref)
        # Keep the first record (the call) and recent results/notifications.
        refs = refs[:1] + refs[-7:] if len(refs) > 8 else refs
        order = record_order(record)
        if previous:
            prior = json.loads(previous['summary'])
            event = merge_event(event, prior) if order < prior.get('_record_order', '') else merge_event(prior, event)
            event['_record_order'] = max(order, prior.get('_record_order', ''))
        else:
            event['_record_order'] = order
        if event.get('agent_id') and event.get('lane') == 'tools' and event.get('category') == 'AGENT':
            db.execute('INSERT INTO agents VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET '
                       "agent_type=excluded.agent_type WHERE excluded.agent_type!=''",
                       (event['agent_id'], event['call_id'], event.get('agent_type', '')))
        summary = dict(event)
        for field in ('detail', 'input', 'output', 'notification'):
            if field in summary:
                summary[field] = str(summary.get(field, ''))[:180]
        summary['detail_available'] = True
        db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)',
                   (event['id'], event['time'], event['turn_id'], json.dumps(summary), json.dumps(refs)))

    def full_event(self, db, key, allowed, reference_budget=16):
        row = db.execute('SELECT refs FROM events WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError('Event is no longer indexed')
        event, artifacts, records = None, [], []
        turn = json.loads(db.execute('SELECT summary FROM events WHERE id=?', (key,)).fetchone()['summary']).get('turn_id', '')
        for path, offset, length, digest in json.loads(row['refs']):
            if path not in allowed:
                continue
            with self.open_source(path) as source:
                source.stream.seek(offset)
                raw = source.stream.read(min(length, MAX_LINE))
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Indexed source record changed')
            record = json.loads(raw)
            context = self.context(db, path)
            matches = [e for e in normalize(record, {**context, 'turn': turn}) if e['id'] == key]
            if not matches:
                raise ValueError('Indexed event identity changed')
            records.append((record_order(record), offset, matches[0], record))
        for _, _, incoming, record in sorted(records, key=lambda entry: entry[:2]):
            event = merge_event(event, incoming) if event else incoming
            if incoming.get('reply_phase') == 'Final' and incoming.get('source') == 'transcript':
                cwd = record.get('cwd')
                references = final_references(self.final_text(record))[:reference_budget]
                reference_budget -= len(references)
                artifacts = [a for ref in references
                             if (a := verified_reference(Path(cwd) if isinstance(cwd, str) else None, ref))]
        if event is None:
            raise FileNotFoundError('No readable event source')
        if artifacts:
            event['artifacts'] = artifacts
        return event

    @staticmethod
    def final_text(record):
        message = record.get('message') if isinstance(record.get('message'), dict) else {}
        content = message.get('content') if isinstance(message.get('content'), list) else []
        return '\n'.join(b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))

    def snapshot(self, before=None, detail=None, anchor=None):
        with self.lock:
            if self.disk_limit_bytes is not None and self.disk_limit_bytes < 32768 and self.path.stat().st_size == 0:
                return {'session_id': self.session, 'events': [], 'version': 'capacity', 'log_state': 'available',
                        'indexing': True, 'has_earlier': True, 'next_cursor': None,
                        'warnings': ['Index cache capacity reached. Close unused viewers to free rebuildable indexes; source logs are unchanged.']}
            self.initialize()
            if (self.capacity_limit_hit is not None and self.disk_limit_bytes is not None
                    and self.disk_limit_bytes <= self.capacity_limit_hit
                    and time.monotonic() < self.capacity_retry_at):
                return self._snapshot(before, detail, anchor, capacity_limited=True)
            try:
                result = self._snapshot(before, detail, anchor)
                self.capacity_limit_hit = None
                return result
            except IndexReset:
                with self.connect() as db:
                    self.clear(db)
                self.notices.add('A transcript changed on disk after indexing. The disposable index was rebuilt; source logs were not modified.')
                return self._snapshot(before, detail, anchor)
            except sqlite3.OperationalError as exc:
                self.validation.clear()
                # Python 3.10 lacks sqlite_errorcode; keep its exact FULL message fallback.
                if (getattr(exc, 'sqlite_errorcode', None) != 13
                        and str(exc) != 'database or disk is full'):
                    raise
                self.capacity_limit_hit = self.disk_limit_bytes
                self.capacity_retry_at = time.monotonic() + 60
                # The failed transaction rolls back; keep serving the last safe
                # snapshot rather than deleting active history or growing forever.
                return self._snapshot(before, detail, anchor, capacity_limited=True)

    def availability(self):
        available, missing, pending = {}, [], []
        for path in self.paths:
            try:
                with self.open_source(path) as source:
                    available[path] = source.info
            except FileNotFoundError:
                missing.append(path)
            except SourcePending:
                pending.append(path)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError('Registered transcript was replaced by a symlink') from None
                raise
        return available, missing, pending

    def release(self):
        # Only consulted when no main transcript is readable.
        return next(filter(None, (read_release(p) for p in self.paths[:7] if not agent_of(p))), None)

    def _snapshot(self, before=None, detail=None, anchor=None, capacity_limited=False):
        boundary = decode_cursor(before) if before else None
        with self.connect() as db:
            available, missing, pending_paths = self.availability()
            indexed_before = db.execute('SELECT path FROM sources').fetchall()
            warnings = sorted(self.notices)
            if capacity_limited:
                warnings.append('Index cache capacity reached. New indexing is paused; close unused viewers to free cache. Source logs are unchanged.')
            linked = self.subagents(db, available, warnings) if available else []
            allowed = set(available) | set(linked)
            if detail:
                try:
                    return {'event': self.full_event(db, detail, allowed)}
                except (FileNotFoundError, SourcePending):
                    return {'event': None, 'detail_warning': 'The source record is no longer readable; only the indexed summary remains.'}
            if available:
                state = 'available'
            elif pending_paths or not indexed_before:
                state = 'pending'
            else:
                state = 'missing'
            if state == 'pending' and not indexed_before and not pending_paths:
                release = self.release()
                if release and release['reason'] == 'delete':
                    # Deleted before any page indexed it: nothing remains to show, and nothing will be written.
                    return {'session_id': self.session, 'events': [], 'log_state': 'missing', 'version': 'deleted',
                            'indexing': False, 'has_earlier': False, 'next_cursor': None, 'release': release,
                            'warnings': ['Claude Code deleted this session at ' + release['released_at'] + ' before it was indexed. No history is available.']}
            if state == 'pending' and not indexed_before:
                return {'session_id': self.session, 'events': [], 'log_state': 'pending', 'version': 'pending',
                        'indexing': False, 'has_earlier': False, 'next_cursor': None,
                        'warnings': ['Waiting for Claude Code to write this transcript. Only recorded events are shown.']}
            release = None
            if state == 'missing':
                release = self.release()
                if release and release['reason'] == 'delete':
                    warnings.append('Claude Code deleted this session at ' + release['released_at'] + '. Showing the rebuildable index only; details are unavailable.')
                else:
                    warnings.append('The registered transcript no longer exists. Showing the rebuildable index only; details are unavailable.')
            elif missing:
                warnings.append('A registered segment is unavailable')
            for path, in indexed_before:
                if agent_of(path) and path not in linked:
                    warnings.append('A linked subagent transcript is unavailable')
                    break
            changed = False
            broken = set()
            mains = [p for p in self.paths if p in available][:7]
            for path in ([] if capacity_limited else mains):
                try:
                    changed = self.ingest(db, path) or changed
                except (FileNotFoundError, SourcePending):
                    broken.add(path)
                    warnings.append('A registered segment is unavailable')
            # Dispatches indexed just now may link new subagent transcripts.
            if available and not capacity_limited:
                linked = self.subagents(db, available, [])
                allowed = set(available) | set(linked)
            position = db.execute("SELECT value FROM metadata WHERE key='source_position'").fetchone()
            position = position[0] if position else 0
            count = min(7 - len(mains), len(linked))
            scheduled = [linked[(position + i) % len(linked)] for i in range(count)] if linked else []
            if not capacity_limited and linked:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('source_position',?)", ((position + count) % len(linked),))
            for path in ([] if capacity_limited else scheduled):
                try:
                    changed = self.ingest(db, path) or changed
                except (FileNotFoundError, SourcePending, ValueError):
                    # IndexReset is reserved for main sources; a bad subagent file is only skipped.
                    broken.add(path)
                    warnings.append('A linked subagent transcript is unavailable')
            sources = mains + linked
            indexed = db.execute('SELECT * FROM sources ORDER BY path').fetchall()
            # Backfill one bounded historical chunk only while a page is open.
            for row in ([] if capacity_limited else reversed(indexed)):
                if row['start'] > 0 and row['path'] in allowed:
                    try:
                        changed = self.ingest(db, row['path'], backward=True) or changed
                        break
                    except (FileNotFoundError, SourcePending):
                        warnings.append('A registered segment is unavailable')
            indexed = db.execute('SELECT * FROM sources ORDER BY path').fetchall()
            rows_by_path = {row['path']: row for row in indexed}
            if self.validation:
                warnings.append('Revalidating saved history after a storage change. Cached summaries are not yet reverified.')
            pending = False
            for path in sources:
                row = rows_by_path.get(path)
                try:
                    _, info = locate(path)
                except (OSError, ValueError):
                    continue
                if path in broken:
                    continue
                # A trailing partial line is not unfinished indexing; it is re-read when the file grows.
                pending |= (row is None or row['identity'] != source_identity(info) or row['start'] > 0
                            or (info.st_size > row['offset'] and info.st_size != row['tail_size']))
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
            rows = db.execute('SELECT * FROM events ' + where + ' ORDER BY stamp DESC,id DESC LIMIT ?',
                              [*args, PAGE_EVENTS + 1]).fetchall()
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
            # Artifact metadata is verified lazily from final public replies only.
            reference_budget = 32
            for event in reversed(events):
                if event.get('reply_phase') == 'Final' and event.get('source') == 'transcript' and reference_budget and state == 'available':
                    try:
                        event['artifacts'] = self.full_event(db, event['id'], allowed, min(16, reference_budget)).get('artifacts', [])
                    except (OSError, ValueError, KeyError, SourcePending):
                        event['artifacts'] = []
                    reference_budget -= min(16, reference_budget)
            cursor = encode_cursor(selected[-1]['stamp'], selected[-1]['id']) if selected else before
            updated = max((info.st_mtime for info in available.values()), default=0)
            artifact_version = hashlib.sha256(json.dumps([e.get('artifacts', []) for e in events]).encode()).hexdigest()[:12]
            skipped = sum(row['skipped'] for row in indexed)
            foreign = sum(row['foreign_records'] for row in indexed)
            return {'session_id': self.session, 'source': 'Registered Claude Code transcript',
                    'source_updated': datetime.fromtimestamp(updated, timezone.utc).isoformat() if updated else '',
                    'version': str(db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0]) + ':' + str(before) + ':' + str(anchor) + ':' + artifact_version + ':' + state,
                    'events': events, 'limit': PAGE_EVENTS, 'turn_limit': PAGE_TURNS, 'log_state': state,
                    'next_cursor': cursor if has_earlier and not pending else None, 'has_earlier': has_earlier,
                    'indexing': pending, 'skipped': skipped, 'foreign_records': foreign, 'subagents': len(linked),
                    'tail_limited': has_earlier, 'warnings': sorted(set(warnings)), 'release': release,
                    'coverage': 'Recent 20 turns, at most 300 events per page. Load earlier for older history. Source logs are read-only.'}
