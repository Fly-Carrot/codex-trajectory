import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import sqlite3
import os
from contextlib import contextmanager

import test_trace_history as fixtures
from src.trace_viewer.broker import Registry
from src.trace_viewer import launcher
from src.trace_viewer.sources import Source, SourcePending

try:
    from compression import zstd
    compress = zstd.compress
except ImportError:
    try:
        from backports import zstd
        compress = zstd.compress
    except ImportError:
        compress = None


class SegmentTests(unittest.TestCase):
    setUp = fixtures.HistoryTests.setUp
    log = fixtures.HistoryTests.log

    def device_change(self, count=1):
        payload = self.log('a', 'device.jsonl', count=count)
        self.registry.register(payload)
        before = self.registry.snapshot('a')
        reader = self.registry.readers['a'][0]
        with reader.connect() as db:
            row = db.execute('SELECT identity FROM sources').fetchone()
            device, inode = row['identity'].split(':')
            db.execute('UPDATE sources SET identity=?', (f'{int(device)+1}:{inode}',))
        restored = Registry(self.root/'state', self.sessions)
        return payload, before, restored

    def test_restart_device_change_revalidates_identical_records(self):
        payload, before, restored = self.device_change(3)
        original = Path(payload['transcript_path']).read_bytes()
        after = restored.snapshot('a')
        self.assertEqual(before['events'], after['events'])
        self.assertFalse(after['indexing'])
        self.assertEqual(original, Path(payload['transcript_path']).read_bytes())

    def test_device_change_revalidation_is_bounded(self):
        _, before, restored = self.device_change(50)
        with patch('src.trace_viewer.history.WINDOW', 512):
            after = restored.snapshot('a')
            self.assertTrue(after['indexing'])
            for _ in range(100):
                after = restored.snapshot('a')
                if not after['indexing']:
                    break
        self.assertFalse(after['indexing'])
        self.assertEqual(before['events'], after['events'])

    def test_device_change_rejects_modified_proof(self):
        payload, _, restored = self.device_change()
        path = Path(payload['transcript_path'])
        path.write_bytes(path.read_bytes().replace(b'message-0', b'changed-0'))
        with self.assertRaisesRegex(ValueError, 'record changed'):
            restored.snapshot('a')

    def test_device_change_rejects_truncation(self):
        payload, _, restored = self.device_change(3)
        path = Path(payload['transcript_path'])
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
        with self.assertRaisesRegex(ValueError, 'truncated'):
            restored.snapshot('a')

    def test_device_change_rejects_wrong_chat(self):
        payload, _, restored = self.device_change()
        path = Path(payload['transcript_path'])
        path.write_bytes(path.read_bytes().replace(b'"id": "a"', b'"id": "b"'))
        with self.assertRaisesRegex(ValueError, 'Wrong source thread'):
            restored.snapshot('a')

    def test_device_change_does_not_accept_new_inode(self):
        payload, _, restored = self.device_change()
        path = Path(payload['transcript_path'])
        replacement = path.with_suffix('.new')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with self.assertRaisesRegex(ValueError, 'replaced'):
            restored.snapshot('a')

    def test_device_validation_restarts_when_source_changes(self):
        payload, _, restored = self.device_change(50)
        with patch('src.trace_viewer.history.WINDOW', 512):
            self.assertTrue(restored.snapshot('a')['indexing'])
            path = Path(payload['transcript_path'])
            path.write_bytes(path.read_bytes().replace(b'message-0', b'changed-0'))
            with self.assertRaisesRegex(ValueError, 'record changed'):
                restored.snapshot('a')

    def test_device_change_still_reads_appended_events(self):
        payload, _, restored = self.device_change()
        path = Path(payload['transcript_path'])
        row = {'type': 'response_item', 'timestamp': '2026-10-03T01:00:00Z',
               'payload': {'type': 'message', 'id': 'new', 'role': 'user', 'content': 'new'}}
        with path.open('a') as stream:
            stream.write(json.dumps(row)+'\n')
        snap = restored.snapshot('a')
        self.assertEqual({e['detail'] for e in snap['events']}, {'message-0', 'new'})
        self.assertFalse(snap['indexing'])

    def test_device_change_validates_391_small_records_with_append(self):
        payload, _, restored = self.device_change(391)
        path = Path(payload['transcript_path'])
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'padding', 'payload': {}})+'\n')
        self.assertFalse(restored.snapshot('a')['indexing'])

    def test_device_validation_warns_until_quiet_scan_finishes(self):
        payload, _, restored = self.device_change(50)
        path = Path(payload['transcript_path'])
        with patch('src.trace_viewer.history.WINDOW', 512):
            for _ in range(3):
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'padding', 'payload': {}})+'\n')
                snap = restored.snapshot('a')
                self.assertTrue(snap['indexing'])
                self.assertTrue(any('not yet reverified' in w for w in snap['warnings']))
            for _ in range(100):
                snap = restored.snapshot('a')
                if not snap['indexing']:
                    break
        self.assertFalse(snap['indexing'])
        self.assertFalse(snap['warnings'])

    def test_append_during_device_validation_keeps_warning(self):
        payload, _, restored = self.device_change()
        from src.trace_viewer.history import HistoryReader
        original = HistoryReader.revalidate_device
        def validate(reader, db, path, source, old):
            stream = source.stream
            def read(size):
                data = stream.read(size)
                with Path(payload['transcript_path']).open('a') as writer:
                    writer.write(json.dumps({'type': 'padding', 'payload': {}})+'\n')
                return data
            source.stream = Mock(wraps=stream)
            source.stream.read.side_effect = read
            return original(reader, db, path, source, old)
        with patch.object(HistoryReader, 'revalidate_device', validate):
            snap = restored.snapshot('a')
        self.assertTrue(snap['indexing'])
        self.assertTrue(any('not yet reverified' in w for w in snap['warnings']))
        self.assertFalse(restored.snapshot('a')['warnings'])

    def move(self, payload, archive=False, compressed=False):
        path = Path(payload['transcript_path'])
        target = path
        if archive:
            directory = self.root / 'archived_sessions'
            directory.mkdir(exist_ok=True)
            target = directory / path.name
        data = path.read_bytes()
        if compressed:
            if compress is None:
                self.skipTest('Zstandard decoder not installed')
            target = Path(str(target) + '.zst')
            target.write_bytes(compress(data))
            path.unlink()
        else:
            path.rename(target)
        return target

    def test_registered_segments_survive_archive_and_compression(self):
        for archive, compressed in [(False, True), (True, False), (True, True)]:
            with self.subTest(archive=archive, compressed=compressed):
                thread = f't{int(archive)}{int(compressed)}'
                payload = self.log(thread, thread+'.jsonl', prefix=thread)
                self.registry.register(payload)
                before = self.registry.snapshot(thread)
                target = self.move(payload, archive, compressed)
                data = target.read_bytes()
                for _ in range(10):
                    after = self.registry.snapshot(thread)
                    if not after['indexing']:
                        break
                self.assertFalse(after['warnings'])
                self.assertEqual([e['id'] for e in before['events']], [e['id'] for e in after['events']])
                self.assertEqual(self.registry.snapshot(thread, detail=after['events'][0]['id'])['event']['detail'], thread+'-0')
                self.assertEqual(data, target.read_bytes())

    def test_cold_compressed_registration(self):
        payload = self.log('cold', 'cold.jsonl')
        self.move(payload, compressed=True)
        self.registry.register(payload)
        self.assertTrue(self.registry.snapshot('cold')['events'])

    def test_wrong_thread_archive_is_rejected(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        target = self.move(payload, archive=True)
        target.write_text(json.dumps({'type':'session_meta','payload':{'id':'b'}})+'\n')
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_details_resolve_moved_source_before_list_refresh(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        event = self.registry.snapshot('a')['events'][0]
        self.move(payload, archive=True, compressed=True)
        self.assertEqual(self.registry.snapshot('a', detail=event['id'])['event']['detail'], 'message-0')

    def test_restart_and_resume_compressed_archive(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        self.registry.snapshot('a')
        self.move(payload, archive=True, compressed=True)
        restored = Registry(self.root/'state', self.sessions)
        self.assertEqual(restored.snapshot('a')['events'][0]['detail'], 'message-0')
        with patch.object(launcher, 'launch', return_value={'url':'verified'}) as launch:
            self.assertEqual(launcher.resume(self.root/'state', 'a', self.sessions)['url'], 'verified')
            self.assertFalse(launch.call_args.kwargs['auto_open'])

    def test_completed_compressed_history_does_not_replay_after_restart(self):
        payload = self.log('a', 'completed.jsonl', count=80)
        self.move(payload, compressed=True)
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 512):
            for _ in range(100):
                before = self.registry.snapshot('a')
                if not before['indexing']:
                    break
        self.assertFalse(before['indexing'])
        restored = Registry(self.root/'state', self.sessions)
        with patch.object(Source, 'advance', side_effect=AssertionError('completed source replayed')):
            after = restored.snapshot('a')
        self.assertFalse(after['indexing'])
        self.assertEqual(before['events'], after['events'])

    def test_incomplete_compressed_history_is_not_marked_completed(self):
        payload = self.log('a', 'incomplete.jsonl', count=80)
        self.move(payload, compressed=True)
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 512):
            self.assertTrue(self.registry.snapshot('a')['indexing'])
            restored = Registry(self.root/'state', self.sessions)
            self.assertTrue(restored.snapshot('a')['indexing'])

    def test_completed_compressed_marker_invalidates_on_replacement(self):
        payload = self.log('a', 'changed.jsonl')
        path = self.move(payload, compressed=True)
        self.registry.register(payload)
        self.assertFalse(self.registry.snapshot('a')['indexing'])
        source = self.log('a', 'replacement.jsonl', prefix='altered')
        data = Path(source['transcript_path']).read_bytes()
        path.write_bytes(compress(data))
        restored = Registry(self.root/'state', self.sessions)
        with self.assertRaises(ValueError):
            restored.snapshot('a')

    def test_completed_compressed_marker_invalidates_on_ctime_only_change(self):
        payload = self.log('a', 'ctime.jsonl')
        raw = Path(payload['transcript_path']).read_bytes()
        path = self.move(payload, compressed=True)
        def fixed_size(data):
            padding = 1024 - len(data) - 8
            return data + b'\x50\x2a\x4d\x18' + padding.to_bytes(4, 'little') + b'\0' * padding
        path.write_bytes(fixed_size(path.read_bytes()))
        self.registry.register(payload)
        self.assertFalse(self.registry.snapshot('a')['indexing'])
        before = path.stat()
        changed = fixed_size(compress(raw.replace(b'message-0', b'message-1')))
        self.assertEqual(len(changed), before.st_size)
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertNotEqual(path.stat().st_ctime_ns, before.st_ctime_ns)
        with self.assertRaisesRegex(ValueError, 'record changed'):
            self.registry.snapshot('a')
        restored = Registry(self.root/'state', self.sessions)
        with self.assertRaisesRegex(ValueError, 'record changed'):
            restored.snapshot('a')

    def test_second_ctime_change_during_partial_rescan_rechecks_earlier_proofs(self):
        payload = self.log('a', 'rescan.jsonl', count=80)
        raw = Path(payload['transcript_path']).read_bytes()
        path = self.move(payload, compressed=True)
        def padded(data):
            padding = 4096 - len(data) - 8
            return data + b'\x50\x2a\x4d\x18' + padding.to_bytes(4, 'little') + b'\0' * padding
        initial = padded(compress(raw))
        path.write_bytes(initial)
        self.registry.register(payload)
        self.assertFalse(self.registry.snapshot('a')['indexing'])
        info = path.stat()
        path.write_bytes(initial)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with patch('src.trace_viewer.history.WINDOW', 512):
            self.assertTrue(self.registry.snapshot('a')['indexing'])
            path.write_bytes(padded(compress(raw.replace(b'message-0', b'changed-0'))))
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            restored = Registry(self.root/'state', self.sessions)
            with self.assertRaisesRegex(ValueError, 'record changed'):
                restored.snapshot('a')

    def test_completed_marker_rolls_back_with_index_transaction(self):
        payload = self.log('a', 'rollback.jsonl', count=80)
        self.move(payload, compressed=True)
        self.registry.register(payload)
        with patch('src.trace_viewer.history.WINDOW', 512):
            self.assertTrue(self.registry.snapshot('a')['indexing'])
        reader = self.registry.readers['a'][0]
        connect = reader.connect
        @contextmanager
        def fail_commit():
            with connect() as db:
                yield db
                raise RuntimeError('commit failed')
        with patch.object(reader, 'connect', fail_commit):
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                reader.snapshot()
        with connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM completed_sources').fetchone()[0], 0)
        self.assertFalse(reader.compressed_done)
        self.assertFalse(reader.snapshot()['indexing'])

    def test_completed_marker_survives_reader_eviction(self):
        payload = self.log('a', 'eviction.jsonl')
        self.move(payload, compressed=True)
        self.registry.register(payload)
        self.assertFalse(self.registry.snapshot('a')['indexing'])
        self.registry.readers.clear()
        with patch.object(Source, 'advance', side_effect=AssertionError('eviction replayed source')):
            self.assertFalse(self.registry.snapshot('a')['indexing'])

    def test_compressed_forward_scan_is_bounded_and_continues(self):
        payload = self.log('a', 'a.jsonl', count=200)
        self.registry.register(payload)
        self.move(payload, compressed=True)
        with patch('src.trace_viewer.history.WINDOW', 512):
            first = self.registry.snapshot('a')
            self.assertTrue(first['indexing'])
            self.assertLess(len(first['events']), 20)
            for _ in range(200):
                last = self.registry.snapshot('a')
                if not last['indexing']:
                    break
        self.assertFalse(last['indexing'])
        self.assertEqual(len(last['events']), 200)
        self.assertFalse(list((self.root/'state').rglob('*.jsonl')))

    def test_multiple_compressed_segments_are_not_starved(self):
        for i in range(3):
            payload = self.log('a', f'{i}.jsonl', count=40, prefix=str(i))
            self.registry.register(payload)
            self.move(payload, compressed=True)
        with patch('src.trace_viewer.history.WINDOW', 512):
            for _ in range(200):
                snap = self.registry.snapshot('a')
                if not snap['indexing']:
                    break
        self.assertFalse(snap['indexing'])
        self.assertEqual(len(snap['events']), 120)

    def test_archive_root_symlink_is_rejected(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        outside = self.root/'outside'
        outside.mkdir()
        Path(payload['transcript_path']).rename(outside/'a.jsonl')
        (self.root/'archived_sessions').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.registry.snapshot('a')

    def test_compressed_symlink_is_rejected(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        target = self.move(payload, compressed=True)
        outside = self.root/'outside.zst'
        target.rename(outside)
        target.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            self.registry.snapshot('a')

    def test_corrupt_segment_does_not_poison_other_chat(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        target = self.move(payload, compressed=True)
        good = target.read_bytes()
        target.write_bytes(good[:-3])
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')
        self.registry.register(self.log('b', 'b.jsonl'))
        self.assertTrue(self.registry.snapshot('b')['events'])
        target.write_bytes(good)
        self.assertTrue(self.registry.snapshot('a')['events'])

    def test_plain_and_compressed_copies_do_not_duplicate(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        data = Path(payload['transcript_path']).read_bytes()
        self.move(payload, compressed=True)
        Path(payload['transcript_path']).write_bytes(data)
        self.assertEqual(len(self.registry.snapshot('a')['events']), 1)

    def test_detail_scan_has_explicit_budget(self):
        payload = self.log('a', 'a.jsonl', count=40)
        self.move(payload, compressed=True)
        with Source(Path(payload['transcript_path']).resolve(), self.sessions.resolve(), 'a') as source:
            start = source.stream.tell()
            with self.assertRaises(SourcePending):
                source.advance(start + 200, 64)
            self.assertEqual(source.stream.tell() - start, 64)

    def test_compressed_multiframe_and_unicode(self):
        payload = self.log('a', 'a.jsonl', prefix='bird-\u9e1f')
        self.registry.register(payload)
        path = Path(payload['transcript_path'])
        rows = path.read_bytes().splitlines(keepends=True)
        if compress is None:
            self.skipTest('Zstandard decoder not installed')
        Path(str(path)+'.zst').write_bytes(b''.join(compress(row) for row in rows))
        path.unlink()
        self.assertEqual(self.registry.snapshot('a')['events'][0]['detail'], 'bird-\u9e1f-0')

    def test_changed_compressed_representation_cannot_reuse_old_events(self):
        payload = self.log('a', 'a.jsonl', count=2)
        self.registry.register(payload)
        self.registry.snapshot('a')
        target = self.move(payload, compressed=True)
        changed = (json.dumps({'type':'session_meta','payload':{'id':'a'}})+'\n'+
                   json.dumps({'type':'response_item','timestamp':'2026-10-03T00:00:00Z',
                               'payload':{'type':'message','id':'message-0','role':'user','content':'REPLACED'}})+'\n')
        target.write_bytes(compress(changed.encode()))
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_truncated_compressed_representation_cannot_keep_deleted_events(self):
        payload = self.log('a', 'a.jsonl', count=2)
        self.registry.register(payload)
        self.registry.snapshot('a')
        rows = Path(payload['transcript_path']).read_bytes().splitlines(keepends=True)
        target = self.move(payload, compressed=True)
        target.write_bytes(compress(b''.join(rows[:-1])))
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_shifted_header_cannot_hide_old_record_proofs(self):
        payload = self.log('a', 'a.jsonl', count=2)
        self.registry.register(payload)
        self.registry.snapshot('a')
        target = self.move(payload, compressed=True)
        changed = (json.dumps({'type':'session_meta','payload':{'id':'a','padding':'x'*1024}})+'\n'+
                   json.dumps({'type':'response_item','payload':{'type':'message','id':'message-0',
                               'role':'user','content':'REPLACED'}})+'\n')
        target.write_bytes(compress(changed.encode()))
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_failed_detail_does_not_reset_compressed_progress(self):
        payload = self.log('a', 'a.jsonl')
        self.registry.register(payload)
        self.move(payload, compressed=True)
        self.registry.snapshot('a')
        reader = self.registry.readers['a'][0]
        done = dict(reader.compressed_done)
        with patch.object(reader, 'full_event', side_effect=ValueError('bad detail')):
            with self.assertRaises(ValueError):
                self.registry.snapshot('a', detail='bad')
        self.assertEqual(reader.compressed_done, done)

    def test_skippable_frames_have_a_raw_input_budget(self):
        import io, struct
        from src.trace_viewer.sources import decoder
        if compress is None:
            self.skipTest('Zstandard decoder not installed')
        frame = struct.pack('<II', 0x184D2A50, 4096) + b'x'*4096
        with patch('src.trace_viewer.sources.INPUT_BUDGET', 1024):
            stream = decoder(io.BytesIO(frame+compress(b'hello')))
            try:
                with self.assertRaises(ValueError):
                    stream.read(1)
            finally:
                stream.close()


if __name__ == '__main__':
    unittest.main()
