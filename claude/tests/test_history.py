"""Incremental indexing, recovery and isolation on SYNTHETIC transcripts."""
import json
import os
import time
import unittest

import support
from support import Workspace
from history import HistoryReader, PAGE_EVENTS
from server import MAX_LINE, WINDOW
from broker import private_directory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def reader(self, session, paths):
        directory = private_directory(self.ws.state / session)
        return HistoryReader(directory, self.ws.projects, session, [str(p) for p in paths])

    def settle(self, reader, **query):
        for _ in range(200):
            data = reader.snapshot(**query)
            if not data.get('indexing'):
                return data
        self.fail('index did not settle')

    def basic(self, session='s-basic'):
        t = self.ws.transcript(session)
        t.queue(0)
        t.prompt(1, 'Fix the layout')
        t.text(2, 'Looking now')
        t.call(3, 'toolu_1', 'Bash', {'command': 'ls'})
        t.result(4, 'toolu_1', 'a\nb')
        return t

    def test_basic_events_in_order(self):
        t = self.basic()
        t.text(5, 'Done', stop='end_turn', message_id='m2')
        data = self.settle(self.reader(t.session, [t.write()]))
        self.assertEqual([e['category'] for e in data['events']], ['TURN', 'USER', 'ASSISTANT', 'COMMAND', 'ASSISTANT'])
        self.assertEqual(data['log_state'], 'available')
        self.assertEqual(data['events'][3]['state'], 'returned')

    def test_index_from_older_layout_is_rebuilt(self):
        import sqlite3
        t = self.basic('s-upgrade')
        path = t.write()
        directory = private_directory(self.ws.state / t.session)
        db = sqlite3.connect(directory / 'history.sqlite3')
        db.executescript('''CREATE TABLE sources(path TEXT PRIMARY KEY, identity TEXT, start INTEGER, offset INTEGER,
                            turn TEXT, skipped INTEGER, dropping INTEGER, foreign_records INTEGER);
                            CREATE TABLE metadata(key TEXT PRIMARY KEY, value INTEGER);
                            INSERT INTO metadata VALUES('format', 1);''')
        db.commit()
        db.close()
        os.chmod(directory / 'history.sqlite3', 0o600)
        data = self.settle(self.reader(t.session, [path]))
        self.assertEqual(data['events'][-1]['category'], 'COMMAND')

    def test_partial_line_waits_and_does_not_duplicate(self):
        t = self.basic()
        tail = json.dumps(t.text(6, 'Half written', message_id='m3')).encode()
        t.records.pop()
        path = t.write(partial_tail=tail[:40])
        reader = self.reader(t.session, [path])
        first = self.settle(reader)
        self.assertNotIn('Half written', json.dumps(first['events']))
        with open(path, 'ab') as stream:
            stream.write(tail[40:] + b'\n')
        second = self.settle(reader)
        ids = [e['id'] for e in second['events']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum('Half written' in e['detail'] for e in second['events']), 1)

    def test_delayed_result_keeps_call_running_then_completes(self):
        t = self.ws.transcript('s-delay')
        t.prompt(1, 'Run it')
        t.call(2, 'toolu_9', 'Bash', {'command': 'sleep 1'})
        reader = self.reader(t.session, [t.write()])
        self.assertEqual(self.settle(reader)['events'][-1]['state'], 'running')
        t.append(t.result(3, 'toolu_9', 'done'))
        events = self.settle(reader)['events']
        self.assertEqual(events[-1]['state'], 'returned')
        self.assertEqual(sum(e['id'] == 'call:toolu_9' for e in events), 1)

    def test_duplicate_records_merge_once(self):
        t = self.basic('s-dup')
        record = t.records[3]
        t.records.append(record)
        events = self.settle(self.reader(t.session, [t.write()]))['events']
        self.assertEqual(sum(e['id'] == 'call:toolu_1' for e in events), 1)

    def test_foreign_session_records_are_ignored(self):
        t = self.basic('s-own')
        foreign = dict(t.records[2])
        foreign.update(sessionId='s-other', uuid='zz', message={'id': 'mz', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'FOREIGN'}], 'stop_reason': 'end_turn'})
        t.records.append(foreign)
        data = self.settle(self.reader(t.session, [t.write()]))
        self.assertNotIn('FOREIGN', json.dumps(data['events']))
        self.assertEqual(data['foreign_records'], 1)

    def test_wrong_session_file_is_rejected(self):
        t = self.basic('s-real')
        path = t.write()
        reader = self.reader('s-real', [path])
        t.records[0]['sessionId'] = 's-intruder'
        for record in t.records:
            record['sessionId'] = 's-intruder'
        imposter = self.ws.projects / '-work-demo' / 's-claimed.jsonl'
        imposter.write_text('\n'.join(json.dumps(r) for r in t.records) + '\n')
        with self.assertRaises(ValueError):
            self.reader('s-claimed', [imposter]).snapshot()
        self.assertEqual(self.settle(reader)['log_state'], 'available')

    def test_symlinked_transcript_is_rejected(self):
        t = self.basic('s-target')
        target = t.write()
        link = self.ws.projects / '-work-demo' / 's-link.jsonl'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            self.reader('s-link', [link]).snapshot()

    def test_pending_transcript_is_waiting_not_error(self):
        path = self.ws.projects / '-work-demo' / 's-new.jsonl'
        data = self.reader('s-new', [path]).snapshot()
        self.assertEqual((data['log_state'], data['events']), ('pending', []))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'file-history-snapshot'}) + '\n')
        self.assertEqual(self.reader('s-new', [path]).snapshot()['log_state'], 'pending')

    def test_missing_segment_warns_and_keeps_other_segment(self):
        first = self.basic('s-seg')
        a = first.write()
        second = self.ws.transcript('s-seg', project='-other-dir')
        second.prompt(10, 'Second segment', prompt_id='p2')
        b = second.write()
        reader = self.reader('s-seg', [a, b])
        self.assertEqual(len(self.settle(reader)['events']), 6)
        os.rename(a, a.with_suffix('.moved'))
        data = self.settle(reader)
        self.assertIn('A registered segment is unavailable', data['warnings'])
        self.assertEqual(data['log_state'], 'available')

    def test_deleted_transcript_serves_index_with_warning(self):
        t = self.basic('s-gone')
        path = t.write()
        reader = self.reader(t.session, [path])
        self.settle(reader)
        os.rename(path, path.with_suffix('.moved'))
        data = reader.snapshot()
        self.assertEqual(data['log_state'], 'missing')
        self.assertTrue(data['events'])
        self.assertTrue(any('no longer exists' in w for w in data['warnings']))
        detail = reader.snapshot(detail='call:toolu_1')
        self.assertIsNone(detail['event'])

    def release(self, path, value):
        marker = path.with_name(path.stem + '.desktop-released.json')
        marker.write_text(value if isinstance(value, str) else json.dumps(value))
        return marker

    def test_desktop_delete_marker_is_reported(self):
        t = self.basic('s-deleted')
        path = t.write()
        reader = self.reader(t.session, [path])
        self.settle(reader)
        os.remove(path)
        self.release(path, {'v': 1, 'releasedAt': '2026-10-31T02:32:38.926Z', 'reason': 'delete'})
        data = reader.snapshot()
        self.assertEqual((data['log_state'], data['release']), ('missing', {'reason': 'delete', 'released_at': '2026-10-31T02:32:38.926000Z'}))
        self.assertTrue(data['events'])
        self.assertTrue(any(w.startswith('Claude Code deleted this session at 2026-10-31T02:32:38') for w in data['warnings']))
        self.assertFalse(any('no longer exists' in w for w in data['warnings']))

    def test_delete_before_indexing_does_not_wait_forever(self):
        path = self.ws.transcript('s-never').path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.release(path, {'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete'})
        data = self.reader('s-never', [path]).snapshot()
        self.assertEqual((data['log_state'], data['events'], data['indexing']), ('missing', [], False))
        self.assertIn('before it was indexed', data['warnings'][0])

    def test_unrecognised_release_markers_fall_back(self):
        t = self.basic('s-odd')
        path = t.write()
        reader = self.reader(t.session, [path])
        self.settle(reader)
        os.remove(path)
        for value in ['not json', {'v': 2, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete'},
                      {'v': 1, 'releasedAt': '2026-10-31T02:32:38', 'reason': 'delete'},
                      {'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': '<b>delete</b>'},
                      {'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete', 'pad': 'x' * 5000}]:
            marker = self.release(path, value)
            data = reader.snapshot()
            self.assertIsNone(data['release'], value)
            self.assertTrue(any('no longer exists' in w for w in data['warnings']))
            marker.unlink()
        self.release(path, {'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'archive'})
        data = reader.snapshot()
        self.assertEqual(data['release']['reason'], 'archive')
        self.assertTrue(any('no longer exists' in w for w in data['warnings']))

    def test_symlinked_release_marker_is_ignored(self):
        t = self.basic('s-link')
        path = t.write()
        reader = self.reader(t.session, [path])
        self.settle(reader)
        os.remove(path)
        target = self.ws.root / 'elsewhere.json'
        target.write_text(json.dumps({'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete'}))
        os.symlink(target, path.with_name(path.stem + '.desktop-released.json'))
        self.assertIsNone(reader.snapshot()['release'])

    def test_marker_ignored_while_transcript_exists(self):
        t = self.basic('s-alive')
        path = t.write()
        self.release(path, {'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete'})
        data = self.settle(self.reader(t.session, [path]))
        self.assertEqual((data['log_state'], data['release']), ('available', None))

    def test_rewritten_source_rebuilds_index(self):
        t = self.basic('s-rewrite')
        path = t.write()
        reader = self.reader(t.session, [path])
        self.settle(reader)
        t.records[2]['message']['content'][0]['text'] = 'Changed'
        t.records.append(t.records[2])
        replacement = path.with_suffix('.new')
        replacement.write_text('\n'.join(json.dumps(r) for r in t.records) + '\n')
        os.replace(replacement, path)
        data = self.settle(reader)
        self.assertTrue(any('rebuilt' in w for w in data['warnings']))
        self.assertIn('Changed', json.dumps(data['events']))
        self.assertNotIn('Looking now', json.dumps(data['events']))

    def test_inode_change_with_same_content_is_revalidated(self):
        t = self.basic('s-copy')
        path = t.write()
        reader = self.reader(t.session, [path])
        before = self.settle(reader)
        copy = path.with_suffix('.copy')
        copy.write_bytes(path.read_bytes())
        os.replace(copy, path)
        after = self.settle(reader)
        self.assertEqual([e['id'] for e in before['events']], [e['id'] for e in after['events']])
        self.assertFalse(any('rebuilt' in w for w in after['warnings']))

    def test_oversized_record_is_skipped_and_counted(self):
        t = self.basic('s-big')
        t.records.insert(3, {**t.records[2], 'uuid': 'big', 'message': {'id': 'mb', 'role': 'assistant',
                             'content': [{'type': 'text', 'text': 'x' * (MAX_LINE + 10)}], 'stop_reason': 'tool_use'}})
        data = self.settle(self.reader(t.session, [t.write()]))
        self.assertEqual(data['skipped'], 1)
        self.assertEqual([e['category'] for e in data['events']][-1], 'COMMAND')

    def test_long_history_pages_backward(self):
        t = self.ws.transcript('s-long')
        n = 0
        for turn in range(60):
            t.prompt(n, f'turn {turn}', prompt_id=f'p{turn}')
            for step in range(8):
                n += 1
                t.call(n, f'toolu_{turn}_{step}', 'Bash', {'command': 'echo ' + 'y' * 2000}, message_id=f'm{turn}{step}')
                t.result(n, f'toolu_{turn}_{step}', 'z' * 3000, prompt_id=f'p{turn}')
            n += 1
        path = t.write()
        self.assertGreater(path.stat().st_size, WINDOW)
        reader = self.reader(t.session, [path])
        started = time.monotonic()
        data = self.settle(reader)
        self.assertLess(time.monotonic() - started, 30)
        self.assertLessEqual(len(data['events']), PAGE_EVENTS)
        self.assertTrue(data['has_earlier'])
        seen = {e['id'] for e in data['events']}
        cursor = data['next_cursor']
        while cursor:
            page = reader.snapshot(before=cursor)
            seen |= {e['id'] for e in page['events']}
            cursor = page['next_cursor']
        self.assertEqual(len(seen), 60 * 2 + 60 * 8)

    def test_subagent_linked_by_meta_and_unlinked_ignored(self):
        t = self.ws.transcript('s-parent')
        t.prompt(1, 'Dispatch')
        t.call(2, 'toolu_D', 'Agent', {'subagent_type': 'Explore', 'description': 'probe', 'prompt': 'look'})
        t.result(3, 'toolu_D', 'launched', tool_result={'isAsync': True, 'status': 'async_launched', 'agentId': 'aLinked'})
        t.notification(9, 'toolu_D')
        t.write()
        sub = self.ws.subagent('s-parent', 'aLinked', 'toolu_D')
        sub.prompt(4, 'look')
        sub.call(5, 'toolu_S1', 'Bash', {'command': 'ls'}, message_id='ms')
        sub.result(6, 'toolu_S1', 'ok')
        sub.text(7, 'Found', stop='end_turn', message_id='ms2')
        sub.write()
        helper = self.ws.subagent('s-parent', 'aHelper', 'toolu_UNKNOWN')
        helper.text(5, 'HELPER-ONLY', stop='end_turn', message_id='mh')
        helper.write()
        intruder = self.ws.subagent('s-parent', 'aWrong', 'toolu_D')
        intruder.session = 's-somebody-else'
        intruder.text(5, 'INTRUDER', stop='end_turn', message_id='mi')
        intruder.write()
        data = self.settle(self.reader('s-parent', [t.path]))
        text = json.dumps(data['events'])
        self.assertNotIn('HELPER-ONLY', text)
        self.assertNotIn('INTRUDER', text)
        sub_events = [e for e in data['events'] if e['lane'] == 'subagent']
        self.assertEqual({e['agent_id'] for e in sub_events}, {'aLinked'})
        self.assertEqual(len(sub_events), 2)
        dispatch = next(e for e in data['events'] if e['id'] == 'call:toolu_D')
        self.assertEqual((dispatch['state'], dispatch['lane']), ('completed', 'tools'))
        self.assertFalse([e for e in data['events'] if e['lane'] == 'tools' and e.get('agent_id') == 'aLinked' and e['category'] == 'COMMAND'])

    def test_detail_and_verified_artifacts(self):
        report = self.ws.cwd / 'report.md'
        report.write_text('# Synthetic')
        (self.ws.cwd / 'code.py').write_text('x')
        t = self.basic('s-art')
        t.text(5, 'See [report](report.md), [code](code.py), [outside](/etc/hosts) and [web](https://example.org/a.md)',
               stop='end_turn', message_id='mf')
        reader = self.reader(t.session, [t.write()])
        data = self.settle(reader)
        final = next(e for e in data['events'] if e.get('reply_phase') == 'Final')
        self.assertEqual([a['name'] for a in final['artifacts']], ['report.md'])
        detail = reader.snapshot(detail='call:toolu_1')['event']
        self.assertEqual(detail['output'], 'a\nb')


if __name__ == '__main__':
    unittest.main()
