import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from src.trace_viewer.install import configure, BEGIN, END, strip_rule


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)/'codex home with spaces'
        self.home.mkdir()
        self.rules = '# My rules\nKeep this exactly.\n'
        (self.home/'AGENTS.md').write_text(self.rules)
        self.hooks = {'description':'Existing', 'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo untouched'}]}]}}
        (self.home/'hooks.json').write_text(json.dumps(self.hooks))

    def test_dry_run_has_no_side_effects(self):
        before = {p.name:p.read_bytes() for p in self.home.iterdir()}
        configure(self.home, dry_run=True)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.home.iterdir()})

    def test_installed_hook_registers_exact_chat_and_returns_nonblocking_link(self):
        configure(self.home)
        sessions = self.home/'sessions'
        sessions.mkdir()
        log = sessions/'synthetic.jsonl'
        log.write_text(json.dumps({'type':'session_meta', 'payload':{'id':'install-smoke'}})+'\n')
        payload = {'hook_event_name':'UserPromptSubmit','session_id':'install-smoke',
                   'transcript_path':str(log), 'prompt':'not forwarded '*1000}
        command = [sys.executable, str(self.home/'codex-trajectory/codex-trajectory'), '--hook']
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=12)
        connection = json.loads((self.home/'codex-trajectory/runtime/install-smoke/connection.json').read_text())
        self.addCleanup(lambda: os.kill(connection['pid'], signal.SIGTERM))
        response = json.loads(result.stdout)
        self.assertNotIn('decision', response)
        self.assertIn(connection['url'], response['hookSpecificOutput']['additionalContext'])
        result = subprocess.run(command, input='malformed', capture_output=True, text=True, check=True, timeout=12)
        self.assertEqual(json.loads(result.stdout), {'suppressOutput':True})

    def test_install_idempotent_preserves_other_hooks_and_rules(self):
        configure(self.home)
        first = (self.home/'AGENTS.md').read_bytes()
        configure(self.home)
        self.assertEqual(first, (self.home/'AGENTS.md').read_bytes())
        self.assertTrue(first.decode().startswith(self.rules))
        hooks = json.loads((self.home/'hooks.json').read_text())
        self.assertEqual(hooks['hooks']['Stop'], self.hooks['hooks']['Stop'])
        self.assertEqual(len(hooks['hooks']['UserPromptSubmit']), 1)
        self.assertNotIn('PreToolUse', hooks['hooks'])
        self.assertTrue(list((self.home/'codex-trajectory/backups').iterdir()))

    def test_uninstall_preserves_edits_and_removes_only_owned_entries(self):
        configure(self.home)
        with (self.home/'AGENTS.md').open('a') as f:
            f.write('\nNew custom rule\n')
        configure(self.home, uninstall=True)
        rules = (self.home/'AGENTS.md').read_text()
        self.assertNotIn(BEGIN, rules)
        self.assertIn('New custom rule', rules)
        self.assertTrue(rules.startswith(self.rules))
        hooks = json.loads((self.home/'hooks.json').read_text())
        self.assertEqual(hooks['hooks']['Stop'], self.hooks['hooks']['Stop'])
        self.assertEqual(hooks['hooks']['UserPromptSubmit'], [])

    def test_uninstall_restores_original_rule_bytes_when_unchanged(self):
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual((self.home/'AGENTS.md').read_text(), self.rules)

    def test_reinstall_preserves_outside_edits_on_uninstall(self):
        path = self.home/'AGENTS.md'
        configure(self.home)
        changed = 'New prefix\r\n' + path.read_text().replace('Keep this exactly.', 'Updated rule.') + '\nNew suffix\n'
        path.write_bytes(changed.encode())
        expected = strip_rule(changed).encode()
        configure(self.home)
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual(path.read_bytes(), expected)

    def test_reinstall_restores_exact_bytes_when_only_owned_block_changes(self):
        path = self.home/'AGENTS.md'
        for original in (b'', b'No final newline', b'\xef\xbb\xbf# Rules\r\nKeep tabs\t\r\n'):
            with self.subTest(original=original):
                path.write_bytes(original)
                configure(self.home)
                text = path.read_bytes().decode()
                start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
                path.write_bytes((text[:start] + '\nChanged owned instructions\n' + text[end:]).encode())
                configure(self.home)
                configure(self.home)
                configure(self.home, uninstall=True)
                self.assertEqual(path.read_bytes(), original)

    def test_legacy_reinstall_snapshot_cannot_authorize_restoring_old_prose(self):
        path = self.home/'AGENTS.md'
        configure(self.home)
        changed = path.read_text() + '\nUser rule added before legacy upgrade\n'
        path.write_text(changed)
        manifest = self.home/'codex-trajectory/installation.json'
        old = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({key: old[key] for key in
            ('hook_command', 'original_rules', 'uninstalled')} | {'installed_rules': changed}))
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual(path.read_bytes(), strip_rule(changed).encode())

    def test_new_install_cycle_uses_current_original_bytes(self):
        path = self.home/'AGENTS.md'
        configure(self.home)
        configure(self.home, uninstall=True)
        original = b'New rules after uninstall\r\n'
        path.write_bytes(original)
        configure(self.home)
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual(path.read_bytes(), original)

    def test_moved_block_does_not_erase_changed_surrounding_bytes(self):
        path = self.home/'AGENTS.md'
        configure(self.home)
        text = path.read_text()
        block = text[text.index(BEGIN):text.index(END) + len(END)]
        changed = block + strip_rule(text)
        path.write_text(changed)
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual(path.read_bytes(), strip_rule(changed).encode())

    def test_legacy_unchanged_rules_still_restore_exact_bytes(self):
        path = self.home/'AGENTS.md'
        original = b'\xef\xbb\xbfOriginal\r\n'
        path.write_bytes(original)
        configure(self.home)
        manifest = self.home/'codex-trajectory/installation.json'
        old = json.loads(manifest.read_text())
        old.pop('original_outside_rules', None)
        manifest.write_text(json.dumps(old))
        configure(self.home)
        configure(self.home, uninstall=True)
        self.assertEqual(path.read_bytes(), original)

    def test_malformed_config_is_not_overwritten(self):
        (self.home/'hooks.json').write_text('{broken')
        with self.assertRaises(ValueError):
            configure(self.home)
        self.assertEqual((self.home/'hooks.json').read_text(), '{broken')

    def test_symlink_rule_is_rejected(self):
        original = self.home/'original.md'
        (self.home/'AGENTS.md').rename(original)
        (self.home/'AGENTS.md').symlink_to(original)
        with self.assertRaises(ValueError):
            configure(self.home)
        self.assertEqual(original.read_text(), self.rules)


if __name__ == '__main__':
    unittest.main()
