"""Installer boundaries in an isolated, SYNTHETIC Claude home."""
import json
import os
import shlex
import subprocess
import unittest

import support
from support import Workspace, payload
import install
import launcher


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.home = self.ws.home
        self.settings = self.home / 'settings.json'
        self.original = {
            'theme': 'dark', 'permissions': {'allow': ['Bash(ls:*)']}, 'enabledPlugins': {'x@y': True},
            'mcpServers': {'local': {'command': 'serve'}},
            'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'other-start'}]}],
                      'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard'}]}]}}
        self.settings.write_text(json.dumps(self.original, indent=4))
        os.chmod(self.settings, 0o644)

    def load(self):
        return json.loads(self.settings.read_text())

    def owned(self, data):
        return [h for event in install.EVENTS for g in data['hooks'].get(event, []) for h in g['hooks']
                if 'claude-trajectory' in h.get('command', '')]

    def test_dry_run_writes_nothing(self):
        before = self.settings.read_bytes()
        result = install.configure(self.home, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertIn('add SessionStart hook', result['changes'])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.home / 'claude-trajectory').exists())
        self.assertFalse((self.home / 'commands').exists())

    def test_install_is_idempotent_and_preserves_other_settings(self):
        install.configure(self.home)
        once = self.load()
        install.configure(self.home)
        twice = self.load()
        self.assertEqual(once, twice)
        self.assertEqual(len(self.owned(twice)), 2)
        for key in ('theme', 'permissions', 'enabledPlugins', 'mcpServers'):
            self.assertEqual(twice[key], self.original[key])
        self.assertEqual(twice['hooks']['PreToolUse'], self.original['hooks']['PreToolUse'])
        self.assertEqual(twice['hooks']['SessionStart'][0], self.original['hooks']['SessionStart'][0])
        self.assertEqual(oct(self.settings.stat().st_mode & 0o777), '0o644')
        runtime = self.home / 'claude-trajectory'
        self.assertEqual(oct(runtime.stat().st_mode & 0o777), '0o700')
        backups = list((runtime / 'backups').iterdir())
        self.assertTrue(backups)
        self.assertTrue(all(oct(p.stat().st_mode & 0o777) == '0o600' for p in backups))
        self.assertIn('owned command file', (self.home / 'commands' / 'trajectory.md').read_text())

    def test_uninstall_removes_only_owned_entries(self):
        install.configure(self.home)
        install.configure(self.home, uninstall=True)
        after = self.load()
        self.assertEqual(after, self.original)
        self.assertFalse((self.home / 'commands' / 'trajectory.md').exists())
        self.assertTrue((self.home / 'claude-trajectory' / 'app' / 'launcher.py').exists())

    def test_foreign_command_file_is_never_overwritten(self):
        (self.home / 'commands').mkdir()
        (self.home / 'commands' / 'trajectory.md').write_text('mine')
        with self.assertRaises(ValueError):
            install.configure(self.home)
        install.configure(self.home, command=False)
        self.assertEqual((self.home / 'commands' / 'trajectory.md').read_text(), 'mine')
        install.configure(self.home, uninstall=True)
        self.assertEqual((self.home / 'commands' / 'trajectory.md').read_text(), 'mine')

    def test_rule_block_is_added_once_and_removed_exactly(self):
        rules = self.home / 'CLAUDE.md'
        mine = '# Mine\n\nKeep this.\n'
        rules.write_text(mine)
        self.assertIn('add CLAUDE.md rule block', install.configure(self.home, dry_run=True)['changes'])
        self.assertEqual(rules.read_text(), mine)
        install.configure(self.home)
        once = rules.read_text()
        install.configure(self.home)
        self.assertEqual(rules.read_text(), once)
        self.assertTrue(once.startswith(mine))
        self.assertEqual(once.count(install.BEGIN), 1)
        self.assertIn('CLAUDE_CODE_SESSION_ID', once)
        self.assertNotIn('CODEX', once)
        rules.write_text(once + '\nAdded later.\n')
        install.configure(self.home, uninstall=True)
        self.assertEqual(rules.read_text(), mine + '\nAdded later.\n')
        self.assertTrue(any(p.name.endswith('-CLAUDE.md') for p in (self.home / 'claude-trajectory' / 'backups').iterdir()))

    def test_rule_block_round_trip_and_opt_out(self):
        rules = self.home / 'CLAUDE.md'
        rules.write_text('# Mine\n')
        install.configure(self.home)
        install.configure(self.home, uninstall=True)
        self.assertEqual(rules.read_text(), '# Mine\n')
        install.configure(self.home, rule=False)
        self.assertEqual(rules.read_text(), '# Mine\n')
        rules.unlink()
        install.configure(self.home)
        install.configure(self.home, uninstall=True)
        self.assertEqual(rules.read_text(), '')
        rules.write_text(install.BEGIN + '\n' + install.BEGIN + '\n' + install.END)
        with self.assertRaises(ValueError):
            install.configure(self.home)

    def test_invalid_or_symlinked_settings_stop_install(self):
        self.settings.write_text('{not json')
        with self.assertRaises(ValueError):
            install.configure(self.home)
        self.settings.unlink()
        target = self.ws.root / 'elsewhere.json'
        target.write_text('{}')
        self.settings.symlink_to(target)
        with self.assertRaises(ValueError):
            install.configure(self.home)

    def test_installed_hook_runs_from_isolated_copy(self):
        install.configure(self.home)
        state = self.home / 'claude-trajectory' / 'runtime'
        self.addCleanup(lambda: launcher.stop(state, self.ws.projects) if state.exists() else None)
        command = self.owned(self.load())[1]['command']
        path = self.ws.projects / '-work-demo' / 'inst-one.jsonl'
        run = subprocess.run(shlex.split(command), input=json.dumps(payload('UserPromptSubmit', 'inst-one', path)),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('[Trajectory](http://127.0.0.1:', run.stdout)
        wrapper = self.home / 'claude-trajectory' / 'claude-trajectory'
        link = subprocess.run(shlex.split(command)[:-1] + ['url', '--session-id', 'inst-one'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(link.stdout)['session_id'], 'inst-one', link.stderr)
        self.assertTrue(wrapper.exists())


if __name__ == '__main__':
    unittest.main()
