import json
import sqlite3
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from src.trace_viewer.broker import Registry


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.registry = Registry(self.root / 'state', self.sessions)

    def log(self, thread, name, count=1, prefix='message'):
        path = self.sessions / name
        rows = [{'type': 'session_meta', 'payload': {'id': thread}}]
        for i in range(count):
            rows.append({'type': 'response_item', 'timestamp': f'2026-10-03T00:{i // 60:02}:{i % 60:02}Z',
                         'payload': {'type': 'message', 'id': f'{prefix}-{i}', 'role': 'user', 'content': f'{prefix}-{i}'}})
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return {'hook_event_name': 'UserPromptSubmit', 'session_id': thread, 'transcript_path': str(path)}

    def test_same_thread_keeps_old_segments_and_restart_history(self):
        first = self.log('a', 'first.jsonl', prefix='first')
        second = self.log('a', 'second.jsonl', prefix='second')
        self.registry.register(first)
        self.registry.snapshot('a')
        self.registry.register(second)
        snap = self.registry.snapshot('a')
        self.assertEqual({e['detail'] for e in snap['events']}, {'first-0', 'second-0'})
        restored = Registry(self.root / 'state', self.sessions)
        self.assertEqual({e['detail'] for e in restored.snapshot('a')['events']}, {'first-0', 'second-0'})

    def test_incomplete_index_does_not_issue_pagination_cursor(self):
        self.registry.register(self.log('a', 'backfill.jsonl', count=100))
        with patch('src.trace_viewer.history.WINDOW', 1024):
            snapshot = self.registry.snapshot('a')
        self.assertTrue(snapshot['indexing'])
        self.assertIsNone(snapshot['next_cursor'])

    def test_backfill_does_not_replace_newer_tool_results(self):
        payload = self.log('a', 'chunks.jsonl', count=0)
        path = Path(payload['transcript_path'])
        records = [
            {'type':'response_item', 'timestamp':'2026-10-03T01:00:00Z',
             'payload':{'type':'function_call_output','call_id':'call-a','output':'old result'}},
            {'type':'padding','payload':{'text':'x'*1500}},
            {'type':'response_item', 'timestamp':'2026-10-03T01:01:00Z',
             'payload':{'type':'function_call_output','call_id':'call-a','output':'new result'}},
        ]
        with path.open('a') as stream:
            stream.write(''.join(json.dumps(r)+'\n' for r in records))
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 1024):
            for _ in range(10):
                snap = self.registry.snapshot('a')
                if not snap['indexing']:
                    break
        self.assertEqual(snap['events'][0]['output'], 'new result')
        self.assertEqual(self.registry.snapshot('a', detail='call:call-a')['event']['output'], 'new result')

    def test_removed_backfill_segment_does_not_block_other_history(self):
        old = self.log('a', 'old.jsonl', count=100)
        self.registry.register(old)
        with patch('src.trace_viewer.history.WINDOW', 1024):
            self.registry.snapshot('a')
            Path(old['transcript_path']).unlink()
            self.registry.register(self.log('a', 'new.jsonl', prefix='surviving'))
            snap = self.registry.snapshot('a')
        self.assertTrue(any(e['detail']=='surviving-0' for e in snap['events']))
        self.assertFalse(snap['indexing'])
        self.assertTrue(snap['warnings'])

    def test_missing_latest_segment_keeps_surviving_history(self):
        self.registry.register(self.log('a', 'old.jsonl', prefix='surviving'))
        newest = self.log('a', 'new.jsonl')
        self.registry.register(newest)
        self.registry.snapshot('a')
        Path(newest['transcript_path']).unlink()
        snap = self.registry.snapshot('a')
        self.assertTrue(any(e['detail']=='surviving-0' for e in snap['events']))
        self.assertTrue(snap['warnings'])

    def test_non_object_source_header_is_rejected_cleanly(self):
        payload = self.log('a', 'invalid.jsonl')
        self.registry.register(payload)
        self.registry.snapshot('a')
        Path(payload['transcript_path']).write_text('[]\n')
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_fifo_replacement_never_blocks_source_reader(self):
        payload = self.log('a', 'pipe.jsonl')
        self.registry.register(payload)
        path = Path(payload['transcript_path'])
        path.unlink()
        os.mkfifo(path)
        code = ('from src.trace_viewer.broker import Registry; '
                f'r=Registry({str(self.root / "state")!r},{str(self.sessions)!r}); '
                'r.snapshot("a")')
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'ValueError', result.stderr)

    def test_source_scheduling_is_bounded_and_fair(self):
        for i in range(12):
            self.registry.register(self.log('a', f'{i:02}.jsonl', prefix=str(i)))
        from src.trace_viewer.history import HistoryReader
        original = HistoryReader.ingest
        calls = []
        def counted(reader, db, path, backward=False):
            calls.append(path)
            return original(reader, db, path, backward)
        with patch.object(HistoryReader, 'ingest', counted):
            first = self.registry.snapshot('a')
        self.assertLessEqual(len(calls), 8)
        self.assertTrue(first['indexing'])
        for _ in range(4):
            last = self.registry.snapshot('a')
        self.assertEqual(len(last['events']), 12)
        self.assertFalse(last['indexing'])

    def test_index_migrates_sources_without_dropping_column(self):
        self.registry.register(self.log('a', 'source.jsonl'))
        self.registry.snapshot('a')
        reader = self.registry.readers['a'][0]
        with closing(sqlite3.connect(reader.path)) as db, db:
            db.execute('ALTER TABLE sources DROP COLUMN dropping')
        with (self.sessions / 'source.jsonl').open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'new'}})+'\n')
        restored = Registry(self.root / 'state', self.sessions)
        self.assertTrue(restored.snapshot('a')['events'])

    def test_slow_chat_does_not_block_other_chat(self):
        for name in ('a', 'b'):
            self.registry.register(self.log(name, name+'.jsonl'))
            self.registry.snapshot(name)
        reader = self.registry.readers['a'][0]
        original = reader.snapshot
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        reader.snapshot = slow
        one = threading.Thread(target=lambda: self.registry.snapshot('a'))
        two = threading.Thread(target=lambda: (self.registry.snapshot('b'), done.set()))
        one.start()
        entered.wait(1)
        try:
            two.start()
            self.assertTrue(done.wait(.5), 'Another chat was blocked by shared read lock')
        finally:
            release.set()
            one.join(3)
            two.join(3)

    def test_cold_index_lock_does_not_block_other_chat(self):
        for name in ('a', 'b'):
            self.registry.register(self.log(name, name+'.jsonl'))
            self.registry.snapshot(name)
        path = self.registry.readers['a'][0].path
        self.registry.readers.pop('a')
        db = sqlite3.connect(path)
        db.execute('BEGIN EXCLUSIVE')
        entered, done = threading.Event(), threading.Event()
        def read_a():
            entered.set()
            try:
                self.registry.snapshot('a')
            except sqlite3.Error:
                pass
        one = threading.Thread(target=read_a)
        two = threading.Thread(target=lambda: (self.registry.snapshot('b'), done.set()))
        one.start()
        entered.wait(1)
        import time
        time.sleep(.1)
        try:
            two.start()
            self.assertTrue(done.wait(.5), 'Cold index initialization blocked another chat')
        finally:
            db.rollback()
            db.close()
            one.join(3)
            two.join(3)

    def test_index_configuration_error_closes_connection(self):
        self.registry.register(self.log('a', 'source.jsonl'))
        self.registry.snapshot('a')
        reader = self.registry.readers['a'][0]
        connection = Mock()
        connection.execute.side_effect = sqlite3.DatabaseError('corrupt index')
        with patch('src.trace_viewer.history.sqlite3.connect', return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError):
                with reader.connect():
                    pass
        connection.close.assert_called_once_with()

    def test_anchor_restores_event_after_it_leaves_latest_page(self):
        self.registry.register(self.log('a', 'long.jsonl', count=450))
        self.registry.snapshot('a')
        page = self.registry.snapshot('a', anchor='item:message-12')
        self.assertEqual(page['events'][-1]['id'], 'item:message-12')
        self.assertLessEqual(len(page['events']), 300)
        absent = self.registry.snapshot('a', anchor='item:deleted')
        self.assertTrue(absent['warnings'])
        self.assertEqual(absent['events'][-1]['id'], 'item:message-449')

    def test_pages_reach_older_events_without_deleting_source(self):
        payload = self.log('a', 'large.jsonl', count=450)
        path = Path(payload['transcript_path'])
        original = path.read_bytes()
        self.registry.register(payload)
        latest = self.registry.snapshot('a')
        self.assertLessEqual(len(latest['events']), 300)
        self.assertTrue(latest['has_earlier'])
        older = self.registry.snapshot('a', before=latest['next_cursor'])
        ids = {e['id'] for e in latest['events']+older['events']}
        self.assertEqual(len(ids), 450)
        self.assertEqual(path.read_bytes(), original)

    def test_partial_tail_recovers_and_details_are_not_summary(self):
        payload = self.log('a', 'partial.jsonl')
        path = Path(payload['transcript_path'])
        self.registry.register(payload)
        self.registry.snapshot('a')
        raw = json.dumps({'type':'response_item','timestamp':'2026-10-03T01:00:00Z',
                         'payload':{'type':'message','id':'long','role':'user','content':'x'*600}}).encode()
        with path.open('ab') as f:
            f.write(raw[:70])
        self.assertEqual(len(self.registry.snapshot('a')['events']), 1)
        with path.open('ab') as f:
            f.write(raw[70:]+b'\n')
        snap = self.registry.snapshot('a')
        event = next(e for e in snap['events'] if e['id']=='item:long')
        self.assertEqual(len(event['detail']), 180)
        full = self.registry.snapshot('a', detail='item:long')['event']
        self.assertEqual(len(full['detail']), 600)

    def test_twenty_turns_and_exact_duplicate_segments(self):
        payload = self.log('a', 'turns.jsonl', count=0)
        path = Path(payload['transcript_path'])
        with path.open('a') as f:
            for i in range(30):
                f.write(json.dumps({'type':'event_msg','timestamp':f'2026-10-03T00:{i:02}:00Z',
                    'payload':{'type':'task_started','turn_id':str(i)}})+'\n')
                f.write(json.dumps({'type':'response_item','timestamp':f'2026-10-03T00:{i:02}:01Z',
                    'payload':{'type':'message','role':'user','content':str(i)}})+'\n')
        self.registry.register(payload)
        copy = self.sessions/'copy.jsonl'
        copy.write_bytes(path.read_bytes())
        self.registry.register({**payload,'transcript_path':str(copy)})
        latest = self.registry.snapshot('a')
        self.assertEqual(len(latest['events']), 40)
        self.assertEqual(len({e['turn_id'] for e in latest['events']}), 20)
        older = self.registry.snapshot('a', before=latest['next_cursor'])
        self.assertEqual(len(older['events']), 20)

    def test_bounded_backfill_and_restart_reuses_index(self):
        payload = self.log('a', 'backfill.jsonl', count=100)
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 1024):
            for _ in range(40):
                snapshot = self.registry.snapshot('a')
                if not snapshot['indexing']:
                    break
            self.assertFalse(snapshot['indexing'])
            self.assertEqual(len(snapshot['events']), 100)
            restored = Registry(self.root/'state', self.sessions)
            again = restored.snapshot('a')
            self.assertFalse(again['indexing'])
            self.assertEqual({e['id'] for e in snapshot['events']}, {e['id'] for e in again['events']})

    def test_wrong_thread_and_replaced_source_are_rejected(self):
        payload = self.log('a', 'source.jsonl')
        path = Path(payload['transcript_path'])
        self.registry.register(payload)
        self.registry.snapshot('a')
        before = path.read_bytes()
        path.write_text(before.decode().replace('message-0','CHANGED-0'))
        with self.assertRaises(ValueError):
            self.registry.snapshot('a', detail='item:message-0')
        wrong = self.log('other', 'wrong.jsonl')
        with self.assertRaises(ValueError):
            self.registry.register({**wrong, 'session_id':'a'})

    def test_backfill_crosses_a_record_larger_than_window(self):
        payload = self.log('a', 'oversize.jsonl')
        path = Path(payload['transcript_path'])
        with path.open('a') as f:
            f.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':'x'*10000}})+'\n')
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 1024), patch('src.trace_viewer.history.MAX_LINE', 800):
            for _ in range(40):
                snap = self.registry.snapshot('a')
                if not snap['indexing']:
                    break
            self.assertFalse(snap['indexing'])
            self.assertTrue(any(e['id']=='item:message-0' for e in snap['events']))
