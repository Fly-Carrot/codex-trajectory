"""Shared broker, launcher, recovery and hook behavior on SYNTHETIC transcripts."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit

import support
from support import Workspace, payload, ROOT
import broker
import launcher

ENTRY = ROOT / 'trajectory.py'


def get(url, path, token=None, host=None, origin=None):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    headers = {'Host': host or parts.netloc}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if origin:
        headers['Origin'] = origin
    connection.request('GET', path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


class BrokerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        registry = broker.Registry(self.ws.state, self.ws.projects)
        self.server = broker.BrokerServer(registry)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def register(self, session):
        t = self.ws.transcript(session)
        t.prompt(1, 'Hello <script>alert(1)</script>')
        t.call(2, 'toolu_' + session[-2:], 'Bash', {'command': 'ls'})
        t.write()
        session, entry, _ = self.server.registry.register(payload('UserPromptSubmit', session, t.path))
        return self.server.session_connection(session, entry)

    def test_session_isolation_and_auth(self):
        a, b = self.register('sess-aa'), self.register('sess-bb')
        token_a, token_b = urlsplit(a['url']).fragment, urlsplit(b['url']).fragment
        self.assertNotEqual(token_a, token_b)
        status, body = get(a['url'], '/api/sessions/sess-aa/events', token_a)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['session_id'], 'sess-aa')
        self.assertIn('<script>', json.dumps(data))
        self.assertEqual(get(a['url'], '/api/sessions/sess-bb/events', token_a)[0], 401)
        self.assertEqual(get(a['url'], '/api/sessions/sess-aa/events')[0], 401)
        self.assertEqual(get(a['url'], '/api/sessions/sess-zz/events', token_a)[0], 401)

    def test_host_origin_and_path_checks(self):
        a = self.register('sess-cc')
        token = urlsplit(a['url']).fragment
        self.assertEqual(get(a['url'], '/api/sessions/sess-cc/events', token, host='evil.test')[0], 403)
        self.assertEqual(get(a['url'], '/api/sessions/sess-cc/events', token, origin='http://evil.test')[0], 403)
        self.assertEqual(get(a['url'], '/api/sessions/..%2f..%2fetc/events', token)[0], 404)
        self.assertEqual(get(a['url'], '/api/health')[0], 401)
        status, body = get(a['url'], '/s/sess-cc')
        self.assertEqual(status, 200)
        self.assertIn(b'claude-trajectory', body)

    def test_status_does_not_create_reader(self):
        a = self.register('sess-dd')
        status, body = get(a['url'], '/api/sessions/sess-dd/status', urlsplit(a['url']).fragment)
        self.assertEqual((status, json.loads(body)['log_state']), (200, 'available'))
        self.assertNotIn('sess-dd', self.server.registry.readers)

    def test_status_reports_desktop_deletion(self):
        a = self.register('sess-del')
        path = next(self.ws.projects.glob('*/sess-del.jsonl'))
        os.remove(path)
        token = urlsplit(a['url']).fragment
        self.assertEqual(json.loads(get(a['url'], '/api/sessions/sess-del/status', token)[1])['log_state'], 'not_found')
        path.with_name('sess-del.desktop-released.json').write_text(
            json.dumps({'v': 1, 'releasedAt': '2026-10-31T02:32:38Z', 'reason': 'delete'}))
        self.assertEqual(json.loads(get(a['url'], '/api/sessions/sess-del/status', token)[1])['log_state'], 'deleted')

    def test_registration_rejects_inexact_paths(self):
        good = self.ws.transcript('sess-ee')
        good.prompt(1, 'x')
        good.write()
        outside = self.ws.root / 'sess-ee.jsonl'
        outside.write_bytes(good.path.read_bytes())
        wrong_name = good.path.with_name('other.jsonl')
        wrong_name.write_bytes(good.path.read_bytes())
        link_dir = self.ws.projects / '-linked'
        link_dir.symlink_to(good.path.parent)
        for path in (outside, wrong_name, self.ws.projects / '-work-demo' / '..' / '-work-demo' / 'x' / '..' / 'sess-ee.jsonl',
                     link_dir / 'sess-ee.jsonl', 'relative/sess-ee.jsonl'):
            with self.subTest(path=str(path)):
                with self.assertRaises(ValueError):
                    session, entry, _ = self.server.registry.register(payload('UserPromptSubmit', 'sess-ee', path))
                    # A normalized '..' path may resolve to the exact file; it must then be that file.
                    if Path(entry['log']) == good.path:
                        raise ValueError('normalized to the exact file')
        for event in ('Stop', 'PreToolUse', None):
            with self.assertRaises(ValueError):
                self.server.registry.register(payload(event, 'sess-ee', good.path))
        with self.assertRaises(ValueError):
            self.server.registry.register(payload('UserPromptSubmit', '../sess-ee', good.path))

    def test_pending_registration_then_file_appears(self):
        path = self.ws.projects / '-work-demo' / 'sess-ff.jsonl'
        session, entry, _ = self.server.registry.register(payload('SessionStart', 'sess-ff', path))
        url = self.server.session_connection(session, entry)['url']
        token = urlsplit(url).fragment
        self.assertEqual(json.loads(get(url, '/api/sessions/sess-ff/events', token)[1])['log_state'], 'pending')
        t = self.ws.transcript('sess-ff')
        t.prompt(1, 'now written')
        t.write()
        data = json.loads(get(url, '/api/sessions/sess-ff/events', token)[1])
        self.assertEqual((data['log_state'], data['events'][-1]['category']), ('available', 'USER'))

    def test_restart_rotates_tokens(self):
        a = self.register('sess-gg')
        old = self.server.registry.entries['sess-gg']['token']
        again = broker.Registry(self.ws.state, self.ws.projects)
        self.assertNotEqual(again.entries['sess-gg']['token'], old)
        self.assertEqual(again.entries['sess-gg']['log'], a['log'])


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.addCleanup(lambda: launcher.stop(self.ws.state, self.ws.projects) if self.ws.state.exists() else None)

    def transcript(self, session):
        t = self.ws.transcript(session)
        t.prompt(1, 'Synthetic prompt')
        t.write()
        return t

    def run_cli(self, *args, stdin=''):
        return subprocess.run([sys.executable, '-B', str(ENTRY), '--state-dir', str(self.ws.state),
                               '--projects', str(self.ws.projects), *args], input=stdin, capture_output=True,
                              text=True, timeout=60, env={k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_SESSION_ID'})

    def test_register_url_stop_resume_cycle(self):
        t = self.transcript('cli-one')
        first = json.loads(self.run_cli('register', '--transcript', str(t.path)).stdout)
        self.assertEqual((first['status'], first['session_id'], first['opened']), ('started', 'cli-one', False))
        current = json.loads(self.run_cli('url', '--session-id', 'cli-one').stdout)
        self.assertEqual(current['url'], first['url'])
        self.assertEqual(json.loads(self.run_cli('stop').stdout)['status'], 'stopped')
        time.sleep(.3)
        offline = self.run_cli('url', '--session-id', 'cli-one')
        self.assertNotEqual(offline.returncode, 0)
        resumed = json.loads(self.run_cli('resume', '--session-id', 'cli-one').stdout)
        self.assertNotEqual(urlsplit(resumed['url']).fragment, urlsplit(first['url']).fragment)
        self.assertEqual(urlsplit(resumed['url']).port, urlsplit(first['url']).port)
        old = get(resumed['url'], '/api/sessions/cli-one/events', urlsplit(first['url']).fragment)
        self.assertEqual(old[0], 401)
        self.assertEqual(get(resumed['url'], '/api/sessions/cli-one/events', urlsplit(resumed['url']).fragment)[0], 200)

    def test_register_by_exact_session_id(self):
        self.transcript('cli-two')
        result = json.loads(self.run_cli('register', '--session-id', 'cli-two').stdout)
        self.assertEqual(result['session_id'], 'cli-two')
        missing = self.run_cli('register', '--session-id', 'cli-absent')
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('not found exactly once', missing.stderr)

    def test_port_collision_leaves_occupant_running(self):
        t = self.transcript('cli-three')
        first = json.loads(self.run_cli('register', '--transcript', str(t.path)).stdout)
        port = urlsplit(first['url']).port
        self.run_cli('stop')
        time.sleep(.3)
        occupant = socket.socket()
        occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupant.bind(('127.0.0.1', port))
        occupant.listen(1)
        self.addCleanup(occupant.close)
        resumed = json.loads(self.run_cli('resume', '--session-id', 'cli-three').stdout)
        self.assertNotEqual(urlsplit(resumed['url']).port, port)
        occupant.settimeout(.2)
        with self.assertRaises(socket.timeout):
            occupant.accept()

    def test_two_sessions_share_one_service(self):
        a, b = self.transcript('cli-four'), self.transcript('cli-five')
        first = json.loads(self.run_cli('register', '--transcript', str(a.path)).stdout)
        second = json.loads(self.run_cli('register', '--transcript', str(b.path)).stdout)
        self.assertEqual((first['status'], second['status']), ('started', 'reused'))
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual(urlsplit(first['url']).netloc, urlsplit(second['url']).netloc)

    def test_hook_registers_and_supplies_link_once_per_context(self):
        path = self.ws.projects / '-work-demo' / 'hook-one.jsonl'
        start = self.run_cli('hook', stdin=json.dumps({**payload('SessionStart', 'hook-one', path), 'source': 'startup'}))
        self.assertEqual((start.returncode, start.stdout), (0, ''))
        prompt = json.dumps({**payload('UserPromptSubmit', 'hook-one', path), 'prompt': 'hi', 'permission_mode': 'default'})
        first = self.run_cli('hook', stdin=prompt)
        output = json.loads(first.stdout)
        context = output['hookSpecificOutput']['additionalContext']
        self.assertEqual(output['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('[Trajectory](http://127.0.0.1:', context)
        self.assertNotIn('decision', output)
        self.assertEqual(self.run_cli('hook', stdin=prompt).stdout, '')
        self.run_cli('hook', stdin=json.dumps({**payload('SessionStart', 'hook-one', path), 'source': 'compact'}))
        self.assertIn('additionalContext', self.run_cli('hook', stdin=prompt).stdout)

    def test_first_session_start_before_projects_directory_exists(self):
        # Observed with a fresh CLAUDE_CONFIG_DIR: SessionStart runs before projects/ exists.
        self.ws.projects.rmdir()
        path = self.ws.projects / '-work-demo' / 'hook-fresh.jsonl'
        result = self.run_cli('hook', stdin=json.dumps({**payload('SessionStart', 'hook-fresh', path), 'source': 'startup'}))
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertFalse((self.ws.state / 'hook-errors.log').exists())
        self.assertIn('hook-fresh', broker.read_private(self.ws.state / 'registry.json')['sessions'])

    def test_hook_failures_never_block_and_log_is_bounded(self):
        cases = ['not json', '[]', json.dumps(payload('UserPromptSubmit', '../escape', '/etc/passwd')),
                 json.dumps(payload('UserPromptSubmit', 'hook-two', self.ws.root / 'outside.jsonl')),
                 json.dumps(payload('PreToolUse', 'hook-two', '/x'))]
        for raw in cases:
            result = self.run_cli('hook', stdin=raw)
            self.assertEqual((result.returncode, result.stdout), (0, ''), raw)
        log = self.ws.state / 'hook-errors.log'
        self.assertTrue(log.exists())
        self.assertEqual(oct(log.stat().st_mode & 0o777), '0o600')
        self.assertNotIn('passwd', log.read_text())
        for _ in range(3):
            with open(log, 'a') as stream:
                stream.write('x' * 20000 + '\n')
            self.run_cli('hook', stdin='bad')
        self.assertLessEqual(log.stat().st_size, launcher.ERROR_LOG_LIMIT)

    def test_hook_with_unusable_state_still_exits_cleanly(self):
        self.ws.state.parent.mkdir(exist_ok=True)
        self.ws.state.write_text('not a directory')
        path = self.ws.projects / '-work-demo' / 'hook-three.jsonl'
        result = self.run_cli('hook', stdin=json.dumps(payload('UserPromptSubmit', 'hook-three', path)))
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.ws.state.unlink()

    def test_untrusted_service_record_is_not_sent_credentials(self):
        t = self.transcript('cli-six')
        self.run_cli('register', '--transcript', str(t.path))
        service = broker.read_private(self.ws.state / 'service.json')
        forged = {**service, 'token': 'forged-admin-token'}
        broker.write_private(self.ws.state / 'service.json', forged)
        self.assertIsNone(launcher.service_health(self.ws.state, self.ws.projects))
        broker.write_private(self.ws.state / 'service.json', service)


if __name__ == '__main__':
    unittest.main()
