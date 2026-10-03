import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.trace_viewer import broker, launcher


class BrokerTests(unittest.TestCase):
    def test_loopback_startup_does_not_require_reverse_dns(self):
        with patch('socket.getfqdn', side_effect=AssertionError('Loopback must not use DNS')):
            server = broker.BrokerServer(self.registry)
            self.addCleanup(server.server_close)
            self.assertEqual(server.server_name, '127.0.0.1')

    def test_oversized_state_is_rejected_before_replacing_good_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'state.json'
            broker.write_private(p, {'good':True})
            with self.assertRaises(ValueError):
                broker.write_private(p, {'large':'x'*(2*1024*1024)})
            self.assertEqual(broker.read_private(p), {'good':True})
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.state = self.root / 'state'
        self.now = 1.0
        self.registry = broker.Registry(self.state, self.sessions, lambda: self.now)

    def payload(self, thread, name=None, text='public'):
        log = self.sessions / (name or thread + '.jsonl')
        log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': thread}}) + '\n' +
                       json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'id': thread + '-msg', 'role': 'user', 'content': text}}) + '\n')
        return {'hook_event_name': 'UserPromptSubmit', 'session_id': thread, 'transcript_path': str(log)}

    def serving(self):
        server = broker.BrokerServer(self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_lazy_cache_eviction_and_bounded_readers(self):
        for i in range(10):
            self.registry.register(self.payload('a' + str(i)))
        self.assertEqual(len(self.registry.readers), 0)
        for i in range(10):
            self.registry.snapshot('a' + str(i))
        self.assertEqual(len(self.registry.readers), broker.MAX_READERS)
        self.now += broker.CACHE_TTL
        self.registry.evict()
        self.assertEqual(len(self.registry.readers), 0)
        saved = (self.state / 'registry.json').read_text()
        self.assertNotIn('public', saved)
        self.assertNotIn('events', saved)

    def test_registration_path_swap_keeps_token_and_invalidates_cache(self):
        _, first, _ = self.registry.register(self.payload('a', 'one.jsonl', 'FIRST'))
        before = self.registry.snapshot('a')
        _, second, _ = self.registry.register(self.payload('a', 'two.jsonl', 'SECOND'))
        after = self.registry.snapshot('a')
        self.assertEqual(first['token'], second['token'])
        self.assertNotEqual(before['version'], after['version'])
        self.assertEqual(after['events'][0]['detail'], 'SECOND')
        restored = broker.Registry(self.state, self.sessions)
        self.assertNotEqual(restored.entries['a']['token'], first['token'])
        self.assertEqual(len(restored.readers), 0)

    def test_cross_chat_and_admin_tokens_cannot_read_other_chats(self):
        _, a, _ = self.registry.register(self.payload('a'))
        _, b, _ = self.registry.register(self.payload('b'))
        server = self.serving()
        for token in (a['token'], server.admin_token, 'wrong'):
            with self.assertRaises(HTTPError) as err:
                launcher.request(server.origin, '/api/threads/b/events', token)
            self.assertEqual(err.exception.code, 401)
            err.exception.close()
        result = launcher.request(server.origin, '/api/threads/b/events', b['token'])
        self.assertEqual(result['thread_id'], 'b')
        with self.assertRaises(HTTPError) as err:
            launcher.request(server.origin, '/api/register', a['token'], self.payload('c'))
        err.exception.close()

    def test_status_polling_does_not_keep_reader_alive(self):
        _, entry, _ = self.registry.register(self.payload('a'))
        server = self.serving()
        launcher.request(server.origin, '/api/threads/a/status', entry['token'])
        self.assertEqual(len(self.registry.readers), 0)
        self.registry.snapshot('a')
        self.now += broker.CACHE_TTL
        launcher.request(server.origin, '/api/threads/a/status', entry['token'])
        self.registry.evict()
        self.assertEqual(len(self.registry.readers), 0)

    def test_corrupt_index_returns_controlled_error_without_affecting_other_chat(self):
        _, a, _ = self.registry.register(self.payload('broken'))
        _, b, _ = self.registry.register(self.payload('working'))
        self.registry.snapshot('broken')
        cache = self.registry.readers['broken'][0].path
        self.registry.readers.pop('broken')
        cache.write_bytes(b'not a SQLite index')
        server = self.serving()
        with self.assertRaises(HTTPError) as err:
            launcher.request(server.origin, '/api/threads/broken/events', a['token'])
        self.assertEqual(err.exception.code, 503)
        err.exception.close()
        result = launcher.request(server.origin, '/api/threads/working/events', b['token'])
        self.assertEqual(result['thread_id'], 'working')

    def test_symlink_rotation_never_reads_outside_registered_path(self):
        payload = self.payload('a')
        self.registry.register(payload)
        self.registry.snapshot('a')
        source = Path(payload['transcript_path'])
        other = self.root / 'outside.jsonl'
        source.rename(other)
        source.symlink_to(other)
        with self.assertRaises(ValueError):
            self.registry.snapshot('a')

    def test_registration_and_http_origin_guards(self):
        server = self.serving()
        for extra in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
            with self.assertRaises(HTTPError) as err:
                urlopen(Request(server.origin + '/api/health', headers={'Authorization': 'Bearer '+server.admin_token, **extra}))
            self.assertEqual(err.exception.code, 403)
            err.exception.close()
        payload = self.payload('a')
        outside = self.root / 'outside.jsonl'
        outside.write_text(Path(payload['transcript_path']).read_text())
        with self.assertRaises(HTTPError) as err:
            launcher.request(server.origin, '/api/register', server.admin_token, {**payload, 'transcript_path': str(outside)})
        self.assertEqual(err.exception.code, 400)
        err.exception.close()

    def test_redirects_are_not_followed(self):
        requests = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/leaked')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as err:
                launcher.request(f'http://127.0.0.1:{server.server_port}', '/health', 'private')
            self.assertEqual(err.exception.code, 302)
            err.exception.close()
            self.assertEqual(requests, ['/health'])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_private_state_rejects_symlinks_and_public_modes(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError): broker.private_directory(link)
        os.chmod(target, 0o755)
        with self.assertRaises(ValueError): broker.private_directory(target)

    def test_restart_preserves_port_but_rotates_access_tokens(self):
        payload = self.payload('restart')
        first = launcher.launch(payload, self.state, self.sessions)
        other = launcher.launch(self.payload('other'), self.state, self.sessions)
        os.kill(first['pid'], signal.SIGTERM)
        for _ in range(100):
            if not (self.state / 'service.json').exists(): break
            time.sleep(.02)
        second = launcher.launch(payload, self.state, self.sessions)
        self.addCleanup(lambda: os.kill(second['pid'], signal.SIGTERM))
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertEqual(first['url'].split('#')[0], second['url'].split('#')[0])
        self.assertNotEqual(first['url'], second['url'])
        self.assertEqual(launcher.current_link(self.state, 'restart')['url'], second['url'])
        renewed = launcher.current_link(self.state, 'other')
        self.assertNotEqual(renewed['url'], other['url'])
        self.assertEqual(renewed['thread_id'], 'other')

    def test_untrusted_listener_never_receives_bearer_credentials(self):
        seen = []
        class Impostor(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"instance":"fake","proof":"wrong"}')
        fake = ThreadingHTTPServer(('127.0.0.1', 0), Impostor)
        thread = threading.Thread(target=fake.serve_forever, daemon=True); thread.start()
        try:
            broker.write_private(self.state/'service.json', {'origin':f'http://127.0.0.1:{fake.server_port}',
                                 'token':'sensitive-token', 'instance':'fake', 'pid':1})
            self.assertIsNone(launcher.service_health(self.state, self.sessions))
            self.assertEqual(seen, [None])
        finally:
            fake.shutdown(); fake.server_close(); thread.join(2)

    def test_missing_source_does_not_restart_healthy_service(self):
        payload = self.payload('source')
        first = launcher.launch(payload, self.state, self.sessions)
        self.addCleanup(lambda: os.kill(first['pid'], signal.SIGTERM))
        Path(payload['transcript_path']).unlink()
        self.assertEqual(launcher.service_health(self.state, self.sessions)['pid'], first['pid'])
        # A link is healthy independent of log availability. Its events endpoint
        # must return 503, never silently substitute another registered source.
        from urllib.parse import urlsplit
        url = urlsplit(first['url'])
        with self.assertRaises(HTTPError) as err:
            launcher.request(f'http://127.0.0.1:{url.port}', '/api/threads/source/events', url.fragment)
        self.assertEqual(err.exception.code, 503)
        err.exception.close()


if __name__ == '__main__':
    unittest.main()
