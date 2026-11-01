"""macOS recovery handler contract, without registering any URL handler."""
import unittest
from pathlib import Path

import support
import native


class NativeRecoveryTests(unittest.TestCase):
    def test_uri_accepts_exact_session_only(self):
        self.assertEqual(native.parse_uri('claude-trajectory://reconnect/abc-123'), 'abc-123')
        for bad in ('codex-trajectory://reconnect/abc', 'claude-trajectory://reconnect/../x',
                    'claude-trajectory://reconnect/abc?x=1', 'claude-trajectory://reconnect/abc#t',
                    'claude-trajectory://reconnect/a;rm -rf', 'claude-trajectory://evil/abc',
                    'claude-trajectory://reconnect/'):
            with self.assertRaises(ValueError):
                native.parse_uri(bad)

    def test_cancel_does_not_resume(self):
        calls = []
        result = native.recover('claude-trajectory://reconnect/s1', '/x/claude-trajectory', confirm=lambda s: False,
                                resumer=lambda *a: calls.append(a))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(calls, [])

    def test_recover_opens_only_verified_loopback_link(self):
        opened = []
        url = 'http://127.0.0.1:4000/s/s1#token'
        result = native.recover('claude-trajectory://reconnect/s1', '/x/claude-trajectory', confirm=lambda s: True,
                                resumer=lambda state, session, projects: {'session_id': session, 'url': url},
                                checker=lambda path, session: {'url': url, 'session_id': session}, opener=opened.append)
        self.assertEqual((result['status'], opened), ('opened', [url]))
        for bad in ('http://evil.test:4000/s/s1#t', 'http://127.0.0.1:4000/s/other#t', 'http://127.0.0.1:4000/s/s1'):
            with self.assertRaises((ValueError, RuntimeError)):
                native.recover('claude-trajectory://reconnect/s1', '/x/claude-trajectory', confirm=lambda s: True,
                               resumer=lambda state, session, projects, bad=bad: {'session_id': session, 'url': bad},
                               checker=lambda path, session: None, opener=opened.append)
        self.assertEqual(len(opened), 1)

    def test_recovery_paths_and_identity_are_separate_from_codex(self):
        captured = {}
        native.recover('claude-trajectory://reconnect/s1', '/home/u/.claude/claude-trajectory', confirm=lambda s: True,
                       resumer=lambda state, session, projects: captured.update(state=state, projects=projects) or
                       {'session_id': session, 'url': 'http://127.0.0.1:9/s/s1#t'},
                       checker=lambda path, session: {'url': 'http://127.0.0.1:9/s/s1#t', 'session_id': session},
                       opener=lambda url: None)
        self.assertEqual(captured, {'state': Path('/home/u/.claude/claude-trajectory/runtime'),
                                    'projects': Path('/home/u/.claude/projects')})
        self.assertEqual(native.BUNDLE_ID, 'local.claude-trajectory.recovery')
        self.assertNotIn('codex', native.SCHEME)

    def test_dry_run_installs_nothing(self):
        if native.sys.platform != 'darwin':
            self.skipTest('macOS only')
        result = native.install_app('/nonexistent/claude-trajectory', dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertFalse(Path('/nonexistent').exists())


if __name__ == '__main__':
    unittest.main()
