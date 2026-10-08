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
import contextlib
import io
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.trace_viewer import launcher
from src.trace_viewer.broker import Registry


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.log = self.sessions / 'log.jsonl'
        self.log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': 'vscode', 'thread_source': 'user'}}) + '\n')
        self.payload = {'hook_event_name': 'Stop', 'session_id': 'thread-test', 'transcript_path': str(self.log)}

    def test_identity_and_containment(self):
        for changes in ({'session_id': '../escape'}, {'session_id': 'another'}, {'transcript_path': str(self.root)}, {'hook_event_name': 'SubagentStop'}):
            with self.assertRaises(ValueError):
                launcher.resolve_session({**self.payload, **changes}, self.sessions)

    def registered_state(self):
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        launcher.write_private(state / 'registry.json', {
            'schema': launcher.SERVICE_SCHEMA, 'sessions_root': str(self.sessions.resolve()),
            'threads': {'thread-test': {'log': str(self.log), 'token': 'private-token'}}})
        return state

    def test_resume_uses_surviving_registered_segment(self):
        with self.log.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'id': 'surviving-event', 'role': 'user',
                'content': 'surviving history'}}) + '\n')
        state = self.root / 'state'
        registry = Registry(state, self.sessions)
        registry.register(self.payload)
        latest = self.sessions / 'latest.jsonl'
        latest.write_bytes(self.log.read_bytes())
        registry.register({**self.payload, 'transcript_path': str(latest)})
        latest.unlink()
        self.assertEqual(registry.entries['thread-test']['log'], str(latest))
        self.assertEqual(set(registry.entries['thread-test']['logs']),
                         {str(self.log), str(latest)})
        # History already tolerates a missing latest segment; resume must too.
        snapshot = registry.snapshot('thread-test')
        self.assertEqual([event['id'] for event in snapshot['events']], ['item:surviving-event'])
        self.assertTrue(snapshot['warnings'])
        with patch.object(launcher, 'launch', return_value={'url': 'verified'}) as launch:
            self.assertEqual(launcher.resume(state, 'thread-test', self.sessions), {'url': 'verified'})
        self.assertEqual(launch.call_args.args[0]['transcript_path'], str(self.log))
        self.assertFalse(launch.call_args.kwargs['auto_open'])

    def test_resume_prefers_valid_current_over_registered_history(self):
        state = self.registered_state()
        historical = self.sessions / 'z-history.jsonl'
        historical.write_bytes(self.log.read_bytes())
        value = launcher.read_private(state / 'registry.json')
        value['threads']['thread-test']['logs'] = [str(self.log), str(historical)]
        launcher.write_private(state / 'registry.json', value)
        with patch.object(launcher, 'launch', return_value={'url': 'verified'}) as launch:
            launcher.resume(state, 'thread-test', self.sessions)
        self.assertEqual(launch.call_args.args[0]['transcript_path'], str(self.log))

    def test_resume_revalidates_every_fallback_candidate(self):
        state = self.registered_state()
        value = launcher.read_private(state / 'registry.json')
        entry = value['threads']['thread-test']
        entry['log'] = str(self.sessions / 'missing.jsonl')
        headers = {
            'wrong-thread': {'type': 'session_meta', 'payload': {'id': 'other'}},
            'subagent': {'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': 'subagent'}},
            'exec': {'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': 'exec'}},
            'structured-source': {'type': 'session_meta', 'payload': {'id': 'thread-test', 'source': {'subagent': 'worker'}}},
            'thread-source': {'type': 'session_meta', 'payload': {'id': 'thread-test', 'thread_source': 'agent'}},
            'wrong-type': {'type': 'response_item', 'payload': {'id': 'thread-test'}},
            'non-object': [],
            'non-object-payload': {'type': 'session_meta', 'payload': []},
        }
        candidates = []
        for name, header in headers.items():
            candidate = self.sessions / (name + '.jsonl')
            candidate.write_text(json.dumps(header) + '\n')
            candidates.append((name, candidate))
        for name, raw in (('invalid-json', b'{broken\n'), ('invalid-utf8', b'\xff\n')):
            candidate = self.sessions / (name + '.jsonl')
            candidate.write_bytes(raw)
            candidates.append((name, candidate))
        outside = self.root / 'outside.jsonl'
        outside.write_bytes(self.log.read_bytes())
        candidates.append(('outside-root', outside))
        symlink = self.sessions / 'symlink.jsonl'
        symlink.symlink_to(self.log)
        candidates.append(('leaf-symlink', symlink))
        directory = self.sessions / 'real-parent'
        directory.mkdir()
        (directory / 'log.jsonl').write_bytes(self.log.read_bytes())
        alias = self.sessions / 'alias-parent'
        alias.symlink_to(directory, target_is_directory=True)
        candidates.append(('parent-symlink', alias / 'log.jsonl'))
        fifo = self.sessions / 'fifo.jsonl'
        os.mkfifo(fifo)
        candidates.append(('fifo', fifo))
        candidates.append(('directory', directory))
        for name, candidate in candidates:
            with self.subTest(source=name):
                entry['logs'] = [str(candidate)]
                launcher.write_private(state / 'registry.json', value)
                with patch.object(launcher, 'launch') as launch:
                    with self.assertRaises(ValueError):
                        launcher.resume(state, 'thread-test', self.sessions)
                    launch.assert_not_called()
                # An invalid candidate may be skipped, never used as a source.
                entry['logs'] = [str(self.log), str(candidate)]
                launcher.write_private(state / 'registry.json', value)
                with patch.object(launcher, 'launch', return_value={'url': 'verified'}) as launch:
                    launcher.resume(state, 'thread-test', self.sessions)
                self.assertEqual(launch.call_args.args[0]['transcript_path'], str(self.log))

    def test_resume_does_not_discover_unregistered_same_thread_sources(self):
        state = self.registered_state()
        unregistered = self.sessions / 'rollout-thread-test-unregistered.jsonl'
        unregistered.write_bytes(self.log.read_bytes())
        value = launcher.read_private(state / 'registry.json')
        value['threads']['thread-test'].update({
            'log': str(self.sessions / 'missing-current.jsonl'),
            'logs': [str(self.sessions / 'missing-history.jsonl')]})
        launcher.write_private(state / 'registry.json', value)
        with patch.object(launcher, 'launch') as launch:
            with self.assertRaises(ValueError):
                launcher.resume(state, 'thread-test', self.sessions)
            launch.assert_not_called()

    def test_resume_rejects_source_retargeted_during_validation(self):
        state = self.registered_state()
        target = self.sessions / 'unregistered.jsonl'
        target.write_bytes(self.log.read_bytes())
        resolve = launcher.resolve_session

        def retarget(payload, sessions):
            self.log.unlink()
            self.log.symlink_to(target)
            return resolve(payload, sessions)

        with patch.object(launcher, 'resolve_session', side_effect=retarget), \
             patch.object(launcher, 'launch') as launch:
            with self.assertRaises(ValueError):
                launcher.resume(state, 'thread-test', self.sessions)
            launch.assert_not_called()

    def test_resume_rejects_malformed_logs_before_using_valid_current(self):
        state = self.registered_state()
        value = launcher.read_private(state / 'registry.json')
        for logs in (None, str(self.log), [None], ['']):
            with self.subTest(logs=logs), patch.object(launcher, 'launch') as launch:
                value['threads']['thread-test']['logs'] = logs
                launcher.write_private(state / 'registry.json', value)
                with self.assertRaises(ValueError):
                    launcher.resume(state, 'thread-test', self.sessions)
                launch.assert_not_called()

    def test_resume_surviving_segment_after_real_service_exit(self):
        with self.log.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'id': 'surviving-event', 'role': 'user',
                'content': 'surviving history'}}) + '\n')
        original = self.log.read_bytes()
        state = self.root / 'state'
        first = launcher.launch(self.payload, state, self.sessions)
        try:
            latest = self.sessions / 'latest.jsonl'
            latest.write_bytes(original)
            second = launcher.launch({**self.payload, 'transcript_path': str(latest)}, state, self.sessions)
            self.assertEqual(second['pid'], first['pid'])
        finally:
            os.kill(first['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while (state / 'service.json').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse((state / 'service.json').exists())
        latest.unlink()
        with patch.object(launcher, 'open_browser') as opened:
            result = launcher.resume(state, 'thread-test', self.sessions)
        self.addCleanup(lambda: os.kill(result['pid'], signal.SIGTERM))
        self.assertEqual(result['status'], 'started')
        self.assertNotEqual(result['url'], second['url'])
        self.assertEqual(launcher.current_link(state, 'thread-test')['url'], result['url'])
        self.assertFalse(result['opened'])
        opened.assert_not_called()
        url = launcher.urlsplit(result['url'])
        origin = url._replace(path='', query='', fragment='').geturl()
        snapshot = launcher.request(origin, '/api/threads/thread-test/events', url.fragment)
        self.assertEqual([event['id'] for event in snapshot['events']], ['item:surviving-event'])
        self.assertTrue(snapshot['warnings'])
        entry = launcher.read_private(state / 'registry.json')['threads']['thread-test']
        self.assertEqual(entry['log'], str(self.log))
        self.assertEqual(set(entry['logs']), {str(self.log), str(latest)})
        self.assertEqual(self.log.read_bytes(), original)
        self.assertFalse(latest.exists())

    def test_resume_starts_dead_service_and_returns_verified_fresh_url(self):
        state = self.registered_state()
        directory = state / 'thread-test'
        directory.mkdir(mode=0o700)
        stale = {'thread_id': 'thread-test', 'log': str(self.log),
                 'url': 'http://127.0.0.1:1/t/thread-test#stale'}
        launcher.write_private(directory / 'connection.json', stale)
        with patch.object(launcher, 'open_browser') as opened:
            result = launcher.resume(state, 'thread-test', self.sessions)
        self.addCleanup(lambda: os.kill(result['pid'], signal.SIGTERM))
        self.assertEqual(result['status'], 'started')
        self.assertNotEqual(result['url'], stale['url'])
        self.assertEqual(launcher.current_link(state, 'thread-test')['url'], result['url'])
        self.assertFalse(result['opened'])
        opened.assert_not_called()
        again = launcher.resume(state, 'thread-test', self.sessions)
        self.assertEqual(again['status'], 'reused')
        self.assertEqual(again['url'], result['url'])

    def test_resume_connection_fallback_without_registry(self):
        state = self.registered_state()
        (state / 'registry.json').unlink()
        directory = state / 'thread-test'
        directory.mkdir(mode=0o700)
        launcher.write_private(directory / 'connection.json', {
            'thread_id': 'thread-test', 'log': str(self.log)})
        with patch.object(launcher, 'launch', return_value={'url': 'verified'}) as launch:
            self.assertEqual(launcher.resume(state, 'thread-test', self.sessions), {'url': 'verified'})
        self.assertEqual(launch.call_args.args[0]['transcript_path'], str(self.log))
        self.assertFalse(launch.call_args.kwargs['auto_open'])

    def test_cli_resume_after_real_service_exit(self):
        state = self.root / 'state'
        first = launcher.launch(self.payload, state, self.sessions)
        os.kill(first['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while (state / 'service.json').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse((state / 'service.json').exists())
        output = io.StringIO()
        argv = ['launcher', '--resume', '--thread-id', 'thread-test', '--state-dir', str(state)]
        with patch('sys.argv', argv), patch.dict(os.environ, {'CODEX_HOME': str(self.root)}), contextlib.redirect_stdout(output):
            launcher.main()
        result = json.loads(output.getvalue())
        self.addCleanup(lambda: os.kill(result['pid'], signal.SIGTERM))
        self.assertEqual(result['status'], 'started')
        self.assertNotEqual(result['url'], first['url'])
        self.assertEqual(launcher.current_link(state, 'thread-test')['url'], result['url'])
        self.assertFalse(result['opened'])

    def test_resume_does_not_report_success_without_verified_url(self):
        state = self.registered_state()
        with patch.object(launcher, 'launch', return_value={'status': 'launch_in_progress'}):
            with self.assertRaises(RuntimeError):
                launcher.resume(state, 'thread-test', self.sessions)

    def test_service_health_stale_or_unverified_listener_never_gets_token(self):
        state = self.registered_state()
        launcher.write_private(state / 'service.json', {
            'origin': 'http://127.0.0.1:1', 'token': 'private-admin-token',
            'instance': 'stale-instance', 'pid': 123})
        for response in (ConnectionRefusedError(), {'instance': 'stale-instance', 'proof': 'invalid'}):
            kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with self.subTest(response=response), patch.object(launcher, 'request', **kwargs) as request:
                self.assertIsNone(launcher.service_health(state, self.sessions))
                request.assert_called_once()
                self.assertEqual(request.call_args.args[2], '')
                self.assertTrue(request.call_args.args[1].startswith('/api/identity?nonce='))

    def test_launch_recovers_from_non_json_or_non_object_port_occupant(self):
        seen = []

        class Occupant(BaseHTTPRequestHandler):
            body = b'<html>Unrelated service</html>'

            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.body)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Occupant)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        origin = 'http://127.0.0.1:' + str(server.server_port)
        for index, body in enumerate((b'<html>Unrelated service</html>', b'[]', b'null', b'\xff')):
            with self.subTest(body=body):
                Occupant.body = body
                state = self.root / ('occupied-' + str(index))
                state.mkdir(mode=0o700)
                launcher.write_private(state / 'service.json', {
                    'origin': origin, 'token': 'private-admin-token',
                    'instance': 'stale-instance', 'pid': 123})
                launcher.write_private(state / 'endpoint.json', {'port': server.server_port})
                result = launcher.launch(self.payload, state, self.sessions)
                self.addCleanup(lambda pid=result['pid']: os.kill(pid, signal.SIGTERM))
                self.assertEqual(result['status'], 'started')
                self.assertFalse(result['url'].startswith(origin + '/'))
                self.assertEqual(launcher.current_link(state, 'thread-test')['url'], result['url'])
        self.assertTrue(seen)
        self.assertTrue(all(path.startswith('/api/identity?nonce=') and token is None for path, token in seen))
        self.assertTrue(worker.is_alive())

    def test_service_health_preserves_sessions_root_mismatch(self):
        state = self.registered_state()
        data = {'origin': 'http://127.0.0.1:1', 'token': 'private-admin-token',
                'instance': 'instance', 'pid': 123}
        launcher.write_private(state / 'service.json', data)
        health = {'service': 'trajectory', 'schema': launcher.SERVICE_SCHEMA,
                  'instance': data['instance'], 'pid': data['pid'], 'sessions_root': '/other'}
        with patch.object(launcher, 'service_identity', return_value=True), patch.object(launcher, 'request', return_value=health):
            with self.assertRaisesRegex(ValueError, 'another sessions directory'):
                launcher.service_health(state, self.sessions)

    def test_resume_revalidates_source_and_private_paths(self):
        state = self.registered_state()
        path = state / 'registry.json'
        valid = json.loads(path.read_text())
        for source in ('subagent', {'subagent': 'worker'}):
            self.log.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': 'thread-test', 'source': source}}))
            with patch.object(launcher, 'launch') as launch:
                with self.assertRaises(ValueError):
                    launcher.resume(state, 'thread-test', self.sessions)
                launch.assert_not_called()
        valid['threads']['thread-test']['log'] = str(self.root / 'outside.jsonl')
        (self.root / 'outside.jsonl').write_bytes(self.log.read_bytes())
        launcher.write_private(path, valid)
        with self.assertRaises(ValueError):
            launcher.resume(state, 'thread-test', self.sessions)
        path.unlink()
        path.symlink_to(self.log)
        with self.assertRaises(OSError):
            launcher.resume(state, 'thread-test', self.sessions)
        alias = self.root / 'alias'
        alias.symlink_to(state, target_is_directory=True)
        with self.assertRaises(ValueError):
            launcher.resume(alias, 'thread-test', self.sessions)

    def test_resume_rejects_invalid_or_unknown_identity_before_launch(self):
        state = self.registered_state()
        for thread in (None, '../thread-test', 'thread/other', 'unknown'):
            with self.subTest(thread=thread), patch.object(launcher, 'launch') as launch:
                with self.assertRaises((ValueError, RuntimeError)):
                    launcher.resume(state, thread, self.sessions)
                launch.assert_not_called()
        self.log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'other'}}))
        with patch.object(launcher, 'launch') as launch:
            with self.assertRaises(ValueError):
                launcher.resume(state, 'thread-test', self.sessions)
            launch.assert_not_called()

    def test_resume_rejects_malformed_or_public_registry(self):
        state = self.registered_state()
        path = state / 'registry.json'
        valid = json.loads(path.read_text())
        for value in ([], None, {**valid, 'threads': []},
                      {**valid, 'sessions_root': '/elsewhere'},
                      {**valid, 'threads': {'thread-test': {'log': []}}}):
            launcher.write_private(path, value)
            with self.subTest(value=value), patch.object(launcher, 'launch') as launch:
                with self.assertRaises((ValueError, RuntimeError)):
                    launcher.resume(state, 'thread-test', self.sessions)
                launch.assert_not_called()
        launcher.write_private(path, valid)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            launcher.resume(state, 'thread-test', self.sessions)

    def test_cli_rejects_conflicting_flags_before_effects(self):
        cases = [ ['--resume'], ['--resume', '--url'], ['--resume', '--hook'],
                  ['--resume', '--auto-open'], ['--resume', '--log', 'some-log'],
                  ['--url', '--auto-open'], ['--url', '--log', 'some-log'] ]
        for flags in cases:
            argv = ['launcher', *flags]
            if flags != ['--resume']:
                argv += ['--thread-id', 'thread-test']
            with self.subTest(flags=flags), patch('sys.argv', argv), \
                 patch.object(launcher, 'launch') as launch, \
                 patch.object(launcher, 'current_link') as link, \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    launcher.main()
                launch.assert_not_called()
                link.assert_not_called()

    def test_cli_resume_json_and_safe_diagnostics(self):
        argv = ['launcher', '--resume', '--thread-id', 'thread-test']
        output = io.StringIO()
        with patch('sys.argv', argv), patch.object(launcher, 'resume', return_value={'url': 'fresh'}) as resume, contextlib.redirect_stdout(output):
            launcher.main()
        self.assertEqual(json.loads(output.getvalue()), {'url': 'fresh'})
        resume.assert_called_once()
        errors = io.StringIO()
        with patch('sys.argv', argv), patch.object(launcher, 'resume', side_effect=ValueError('secret-token')), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                launcher.main()
        self.assertNotIn('secret-token', errors.getvalue())

    def test_url_does_not_resume_or_create_state(self):
        state = self.root / 'missing'
        with patch('sys.argv', ['launcher', '--url', '--state-dir', str(state), '--thread-id', 'thread-test']), \
             patch.object(launcher, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                launcher.main()
            launch.assert_not_called()
        self.assertFalse(state.exists())

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
