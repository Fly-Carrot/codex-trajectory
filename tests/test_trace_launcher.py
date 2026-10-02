import importlib.util
import json
import os
from pathlib import Path
import signal
import fcntl
import time
import tempfile
import unittest
import threading

from src.trace_viewer import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.log = self.sessions / 'log.jsonl'
        self.log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': 'vscode', 'thread_source': 'user'}}) + '\n')
        self.payload = {'hook_event_name': 'Stop', 'session_id': 'thread-test', 'transcript_path': str(self.log)}

    def test_identity_and_containment(self):
        for changes in ({'session_id': '../escape'}, {'session_id': 'another'}, {'transcript_path': str(self.root)}, {'hook_event_name': 'SubagentStop'}):
            with self.assertRaises(ValueError):
                launcher.resolve_session({**self.payload, **changes}, self.sessions)

    def test_long_prompt_is_not_forwarded_to_registration(self):
        result = launcher.launch({**self.payload, 'prompt': 'private prompt' * 10000}, self.root/'state', self.sessions)
        self.addCleanup(lambda: os.kill(result['pid'], signal.SIGTERM))
        self.assertIn('url', result)

    def test_prompt_hook_supplies_link_but_stop_does_not(self):
        result = {'url': 'http://127.0.0.1:1234/t/a#private'}
        self.assertIn('hookSpecificOutput', launcher.hook_response({'hook_event_name':'UserPromptSubmit'}, result))
        self.assertNotIn('hookSpecificOutput', launcher.hook_response({'hook_event_name':'Stop'}, result))

    def test_subagent_is_not_opened(self):
        self.log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': {'subagent': 'worker'}}}))
        with self.assertRaises(ValueError):
            launcher.resolve_session(self.payload, self.sessions)

    def test_launcher_reuses_server_and_opens_only_once(self):
        opened = []
        state = self.root / 'state'
        first = launcher.launch(self.payload, state, self.sessions, True, opened.append)
        self.addCleanup(lambda: os.kill(first['pid'], signal.SIGTERM))
        second = launcher.launch(self.payload, state, self.sessions, True, opened.append)
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual(second['status'], 'reused')
        self.assertEqual(len(opened), 1)
        self.assertFalse(second['opened'])
        connection = Path(first['connection_file'])
        self.assertEqual(connection.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(launcher.healthy(connection, 'another-thread'))
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_external_connection_not_probed(self):
        path = self.root / 'connection.json'
        path.write_text(json.dumps({'thread_id': 'thread-test', 'url': 'https://example.com/#token'}))
        self.assertIsNone(launcher.healthy(path, 'thread-test'))

    def test_concurrent_hook_does_not_start_duplicate(self):
        state = self.root / 'state'
        directory = state / 'thread-test'
        directory.mkdir(parents=True)
        os.chmod(state, 0o700)
        os.chmod(directory, 0o700)
        with (state / 'launch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = launcher.launch(self.payload, state, self.sessions)
        self.assertEqual(result['status'], 'launch_in_progress')
        self.assertFalse((directory / 'connection.json').exists())

    def test_waiting_hook_registers_after_short_lock_contention(self):
        state = self.root/'state'
        state.mkdir(mode=0o700)
        with (state/'launch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            timer = threading.Timer(.15, lambda: fcntl.flock(lock, fcntl.LOCK_UN))
            timer.start()
            result = launcher.launch(self.payload, state, self.sessions)
            timer.join()
        self.addCleanup(lambda: os.kill(result['pid'], signal.SIGTERM))
        self.assertIn('url', result)

    def test_new_rollout_does_not_reuse_stale_listener(self):
        state = self.root / 'state'
        first = launcher.launch(self.payload, state, self.sessions)
        newer = self.sessions / 'new.jsonl'
        newer.write_bytes(self.log.read_bytes())
        second = launcher.launch({**self.payload, 'transcript_path': str(newer)}, state, self.sessions)
        self.addCleanup(lambda: os.kill(second['pid'], signal.SIGTERM))
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual(first['url'], second['url'])
        self.assertIsNotNone(launcher.healthy(Path(second['connection_file']), 'thread-test', newer))

    def test_two_chats_share_one_process_without_opening_windows(self):
        state = self.root / 'shared'
        first = launcher.launch(self.payload, state, self.sessions)
        self.addCleanup(lambda: os.kill(first['pid'], signal.SIGTERM))
        other = self.sessions / 'other.jsonl'
        other.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread-other'}}) + '\n')
        second = launcher.launch({**self.payload, 'session_id': 'thread-other', 'transcript_path': str(other)}, state, self.sessions)
        if first['pid'] != second['pid']:
            self.addCleanup(lambda: os.kill(second['pid'], signal.SIGTERM))
        self.assertEqual(first['pid'], second['pid'])
        self.assertFalse(first['opened'])
        self.assertFalse(second['opened'])


if __name__ == '__main__':
    unittest.main()
