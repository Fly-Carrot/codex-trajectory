import json
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

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
