import unittest
import json
import os
import signal
import socket
import tempfile
import time
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor


class NativeRecoveryTests(unittest.TestCase):
    def test_exact_uri_only(self):
        from src.trace_viewer.native import parse_uri
        self.assertEqual(parse_uri('codex-trajectory://reconnect/thread-1'), 'thread-1')
        for url in ('https://reconnect/a', 'codex-trajectory://reconnect/a?x=1',
                    'codex-trajectory://reconnect/a#token', 'codex-trajectory://reconnect/a/b',
                    'codex-trajectory://reconnect/%61', 'codex-trajectory://reconnect/a\n',
                    'codex-trajectory://user@reconnect/a', 'codex-trajectory://reconnect/a;exit'):
            with self.assertRaises(ValueError):
                parse_uri(url)

    def test_cancel_does_not_start_or_open(self):
        from src.trace_viewer.native import recover
        resume, opener = Mock(), Mock()
        result = recover('codex-trajectory://reconnect/a', Path('/private/install'),
                         confirm=lambda thread: False, resumer=resume, opener=opener)
        self.assertEqual(result['status'], 'cancelled')
        resume.assert_not_called()
        opener.assert_not_called()


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        from src.trace_viewer import launcher
        self.launcher = launcher
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root/'codex-trajectory'
        self.state = self.base/'runtime'
        self.sessions = self.root/'sessions'
        self.sessions.mkdir()
        self.pids = set()
        self.addCleanup(self.stop_all)
        self.originals = {}
        self.links = {}
        for thread in ('chat-a', 'chat-b'):
            log = self.sessions/(thread+'.jsonl')
            content = json.dumps({'type':'session_meta','payload':{'id':thread,'source':'vscode'}})+'\n'
            log.write_text(content)
            self.originals[log] = log.read_bytes()
            result = launcher.launch({'hook_event_name':'Stop','session_id':thread,'transcript_path':str(log)},
                                     self.state, self.sessions)
            self.pids.add(result['pid'])
            self.links[thread] = result

    def stop_all(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def stop_service(self):
        self.stop_all()
        for _ in range(100):
            if self.launcher.service_health(self.state, self.sessions) is None:
                return
            time.sleep(.02)
        self.fail('Isolated test service did not stop')

    def recover(self, thread):
        from src.trace_viewer.native import recover
        opened = []
        result = recover('codex-trajectory://reconnect/'+thread, self.base,
                         confirm=lambda t: True, opener=opened.append)
        connection = self.launcher.healthy(self.state/thread/'connection.json', thread)
        self.pids.add(connection['pid'])
        self.assertEqual(result['status'], 'opened')
        self.assertEqual(opened, [connection['url']])
        for log, original in self.originals.items():
            self.assertEqual(log.read_bytes(), original)
        return connection

    def test_restart_rotates_expired_links_and_recovers_exact_chats(self):
        self.stop_service()
        current = self.recover('chat-a')
        parsed = urlsplit(current['url'])
        origin = f'http://127.0.0.1:{parsed.port}'
        for thread in ('chat-a', 'chat-b'):
            old = urlsplit(self.links[thread]['url'])
            with self.assertRaises(HTTPError) as failure:
                self.launcher.request(origin, '/api/threads/'+thread+'/status', old.fragment)
            self.assertEqual(failure.exception.code, 401)
            failure.exception.close()
        other = self.recover('chat-b')
        self.assertEqual(current['pid'], other['pid'])
        with self.assertRaises(HTTPError) as failure:
            self.launcher.request(origin, '/api/threads/chat-b/status', parsed.fragment)
        self.assertEqual(failure.exception.code, 401)
        failure.exception.close()

    def test_port_occupied_recovers_without_stopping_occupant(self):
        old_port = urlsplit(self.links['chat-a']['url']).port
        self.stop_service()
        with socket.socket() as occupant:
            occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupant.bind(('127.0.0.1', old_port))
            occupant.listen()
            current = self.recover('chat-a')
            self.assertNotEqual(urlsplit(current['url']).port, old_port)
            self.assertEqual(occupant.getsockname()[1], old_port)

    def test_simultaneous_chat_recovery_reuses_one_service(self):
        self.stop_service()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(self.recover, ('chat-a', 'chat-b')))
        self.assertEqual(len({v['pid'] for v in values}), 1)
        self.assertEqual({v['thread_id'] for v in values}, {'chat-a', 'chat-b'})

    def test_unknown_chat_never_opens_or_registers(self):
        from src.trace_viewer.native import recover
        before = (self.state/'registry.json').read_bytes()
        opened = Mock()
        with self.assertRaises((ValueError, FileNotFoundError)):
            recover('codex-trajectory://reconnect/unknown', self.base,
                    confirm=lambda t: True, opener=opened)
        opened.assert_not_called()
        self.assertEqual((self.state/'registry.json').read_bytes(), before)

    def test_recovery_broker_ignores_pythonpath_startup_code(self):
        self.stop_service()
        injection = self.root/'untrusted'
        injection.mkdir()
        marker = self.root/'startup-ran'
        (injection/'sitecustomize.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
        with patch.dict(os.environ, {'PYTHONPATH': str(injection)}):
            self.recover('chat-a')
        self.assertFalse(marker.exists(), 'Broker executed code outside its installation')

class NativeBoundaryTests(unittest.TestCase):
    def test_open_only_verified_matching_chat(self):
        from src.trace_viewer.native import recover
        resume = Mock(return_value={'thread_id':'a', 'url':'http://127.0.0.1:1234/t/a#secret'})
        verified = {'thread_id':'a', 'url':'http://127.0.0.1:1234/t/a#secret'}
        opener = Mock()
        result = recover('codex-trajectory://reconnect/a', Path('/private/install'),
                         confirm=lambda thread: True, resumer=resume,
                         checker=lambda path, thread: verified, opener=opener)
        self.assertEqual(result['status'], 'opened')
        opener.assert_called_once_with(verified['url'])

    def test_wrong_or_unverified_result_never_opens(self):
        from src.trace_viewer.native import recover
        for value in ({}, {'thread_id':'b','url':'http://127.0.0.1:1234/t/b#x'},
                      {'thread_id':'a','url':'https://evil.invalid/t/a#x'},
                      {'thread_id':'a','url':'http://127.0.0.1:1234/t/a#x'}):
            opener = Mock()
            with self.assertRaises((ValueError, RuntimeError)):
                recover('codex-trajectory://reconnect/a', Path('/private/install'),
                        confirm=lambda thread: True, resumer=lambda *a, **k: value,
                        checker=lambda *a: None, opener=opener)
            opener.assert_not_called()

    def test_failed_resume_never_opens_old_link(self):
        from src.trace_viewer.native import recover
        opener = Mock()
        with self.assertRaises(RuntimeError):
            recover('codex-trajectory://reconnect/a', Path('/private/install'),
                    confirm=lambda thread: True, resumer=Mock(side_effect=RuntimeError('offline')),
                    opener=opener)
        opener.assert_not_called()


@unittest.skipUnless(sys.platform == 'darwin', 'macOS recovery app')
class NativeInstallTests(unittest.TestCase):
    def test_compile_native_app_under_unicode_path(self):
        from src.trace_viewer.native import install_app
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)/'中文 recovery'
            base.mkdir(mode=0o700)
            (base/'app').mkdir(mode=0o700)
            (base/'app/native.py').write_text('# compile-only test fixture\n')
            def run(command, **kwargs):
                if command[0].endswith('/lsregister'):
                    return subprocess.CompletedProcess(command, 0)
                return real_run(command, **kwargs)
            with patch('src.trace_viewer.native.subprocess.run', side_effect=run):
                result = install_app(base)
            self.assertTrue(Path(result['app']).is_dir())
