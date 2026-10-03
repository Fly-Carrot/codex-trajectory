import json
import os
import shlex
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from src.trace_viewer.events import normalize, preview, redact
from src.trace_viewer.server import RolloutReader


def message(text, phase='final'):
    return {'type': 'response_item', 'timestamp': '2026-10-03T00:00:00Z',
            'payload': {'type': 'message', 'role': 'assistant', 'phase': phase, 'content': text}}


def command(text, code=0, **fields):
    return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
        'type': 'CommandExecution', 'id': 'test', 'command': text, 'exit_code': code, **fields}}}


class SemanticTests(unittest.TestCase):
    def test_credentials_with_quotes_spaces_and_escapes_are_fully_redacted(self):
        for value in ('password="alpha beta gamma"', "token='alpha beta gamma'",
                      r'password="alpha\"beta gamma"', r'password=\"alpha beta gamma\"',
                      r'password=alpha\ beta\ gamma', 'password="alpha beta gamma'):
            with self.subTest(value=value):
                result = redact(value)
                self.assertIn('[redacted]', result)
                for secret in ('alpha', 'beta', 'gamma'):
                    self.assertNotIn(secret, result)

    def test_structured_previews_redact_before_json_escaping(self):
        value = {'items': [{'snippet': 'password="alpha beta gamma"'},
                           {'access_token': ['private-one', {'value': 'private-two'}]}],
                 'api_key': {'value': 'private-three'}, 'password': 123456789,
                 'max_output_tokens': 1200, 'original_token_count': 42}
        before = json.dumps(value)
        for result in (preview(value), redact(json.dumps(value))):
            with self.subTest(result=result):
                for secret in ('alpha', 'beta', 'gamma', 'private-one', 'private-two', 'private-three', '123456789'):
                    self.assertNotIn(secret, result)
                parsed = json.loads(result)
                self.assertEqual(parsed['max_output_tokens'], 1200)
                self.assertEqual(parsed['original_token_count'], 42)
        self.assertEqual(json.dumps(value), before)

    def test_escaped_json_text_credentials_do_not_survive(self):
        value = 'password="alpha \\"beta\\" gamma"'
        for _ in range(3):
            value = json.dumps({'snippet': value})
            result = redact(value)
            for secret in ('alpha', 'beta', 'gamma'):
                self.assertNotIn(secret, result)

    def test_shell_concatenated_quotes_hide_the_entire_credential(self):
        for value in ('password=' + shlex.quote("alpha'beta gamma"),
                      'password=' + shlex.quote("alpha\\'beta gamma"),
                      'token="alpha"\'beta gamma\'', 'password=alpha"beta gamma"'):
            with self.subTest(value=value):
                result = redact(value + ' PUBLIC_SUFFIX')
                for secret in ('alpha', 'beta', 'gamma'):
                    self.assertNotIn(secret, result)
                self.assertIn('PUBLIC_SUFFIX', result)

    def test_search_redacts_selected_fields_without_expanding_allowlist(self):
        raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'Extension', 'kind': 'web.search', 'id': 'search',
            'query': 'token="query secret"', 'internal': 'PRIVATE_INTERNAL',
            'results': [{'type': 'text_result', 'title': 'Public title',
                         'snippet': 'password="synthetic-secret-9281"',
                         'internal': 'PRIVATE_RESULT'},
                        {'type': 'image', 'data': 'PRIVATE_BINARY'}]}}}
        event = normalize(raw, 1)
        serialized = json.dumps(event)
        for secret in ('query secret', 'synthetic-secret-9281', 'PRIVATE_'):
            self.assertNotIn(secret, serialized)
        self.assertIn('Public title', event['output'])

    def test_mcp_redaction_does_not_expose_nontext_result_fields(self):
        raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'McpToolCall', 'server': 'test', 'tool': 'read',
            'arguments': {'password': {'value': 'private-argument'}},
            'internal': 'PRIVATE_INTERNAL', 'result': {'content': [
                {'type': 'text', 'text': 'password="alpha beta gamma"'},
                {'type': 'image', 'data': 'PRIVATE_BINARY'}], 'internal': 'PRIVATE_RESULT'}}}}
        result = json.dumps(normalize(raw, 1))
        for secret in ('private-argument', 'alpha', 'beta', 'gamma', 'PRIVATE_'):
            self.assertNotIn(secret, result)

    def test_redaction_of_large_continuous_text_is_bounded(self):
        result = subprocess.run([sys.executable, '-B', '-c',
            'from src.trace_viewer.events import redact; s=redact("x"*200000); assert len(s)<2500'],
            timeout=2, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_native_search_extension_is_recorded_without_private_fields(self):
        raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'Extension', 'kind': 'web.search', 'id': 'native-search',
            'query': 'Codex documentation', 'internal': 'PRIVATE_INTERNAL',
            'results': [{'type': 'text_result', 'title': 'Docs',
                         'url': 'https://example.com', 'snippet': 'Public summary',
                         'internal': 'PRIVATE_RESULT'}]}}}
        event = normalize(raw, 1)
        self.assertIsNotNone(event)
        self.assertEqual(event['category'], 'SEARCH')
        self.assertEqual(event['call_id'], 'native-search')
        self.assertIn('Codex documentation', event['input'])
        self.assertIn('Public summary', event['output'])
        self.assertNotIn('PRIVATE', json.dumps(event))
        raw['payload']['type'] = 'item_started'
        started = normalize(raw, 2)
        self.assertEqual(started['id'], event['id'])
        self.assertEqual(started['state'], 'running')

    def test_browser_search_words_do_not_create_native_search(self):
        for kind in ('browser.search', 'web.open', 'unknown'):
            raw = {'type': 'event_msg', 'payload': {'type': 'item_completed',
                'item': {'type': 'Extension', 'kind': kind, 'query': 'search'}}}
            self.assertIsNone(normalize(raw, 1))
        self.assertEqual(normalize(command('ego-browser search Codex'), 1)['category'], 'COMMAND')
        raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'McpToolCall', 'server': 'chrome', 'tool': 'search', 'arguments': {}}}}
        self.assertEqual(normalize(raw, 1)['category'], 'MCP')

    def test_plan_and_context_use_only_structured_public_fields(self):
        for item_type, category in [('Plan', 'PLAN'), ('ContextCompaction', 'CONTEXT')]:
            raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': item_type, 'id': 'a', 'text': 'Public plan', 'internal': 'PRIVATE'}}}
            event = normalize(raw, 1)
            self.assertEqual(event['category'], category)
            self.assertNotIn('PRIVATE', json.dumps(event))
        self.assertEqual(normalize(message('Plan: inspect then test'), 1)['category'], 'ASSISTANT')

    def test_reply_phases_and_loaded_skill_are_explicit(self):
        self.assertEqual(normalize(message('hello'), 1)['reply_phase'], 'Final')
        self.assertEqual(normalize(message('working', 'commentary'), 1)['reply_phase'], 'Progress')
        event = normalize(command('cat /workspace/skills/research/SKILL.md'), 1)
        self.assertIn('Skill: research (loaded)', event['badges'])
        for text, code in [('echo /workspace/skills/research/SKILL.md', 0),
                           ('cat /workspace/skills/research/SKILL.md', 1),
                           ('cat /workspace/skills/research/SKILL.md; false', 0)]:
            self.assertFalse(normalize(command(text, code), 1).get('badges'))

    def test_plugin_requires_explicit_structured_provenance(self):
        raw = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'McpToolCall', 'server': 'browser', 'tool': 'inspect',
            'arguments': {'plugin_name': 'untrusted-argument'}}}}
        self.assertFalse(normalize(raw, 1).get('badges'))
        raw['payload']['item']['plugin_name'] = 'Browser'
        self.assertIn('Plugin: Browser', normalize(raw, 1)['badges'])

    def test_artifacts_are_local_final_references_not_tool_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = root/'report.html'
            report.write_text('report')
            log = root/'log.jsonl'
            rows = [{'type': 'session_meta', 'payload': {'id': 'chat', 'cwd': str(root)}},
                    message('[report](report.html)'),
                    message('[progress](report.html)', 'commentary'),
                    {'type': 'response_item', 'payload': {'type': 'function_call_output',
                     'call_id': 'a', 'output': 'Created report.html'}}]
            log.write_text('\n'.join(map(json.dumps, rows))+'\n')
            reader = RolloutReader(log, 'chat')
            snap = reader.snapshot()
            artifacts = [a for e in snap['events'] for a in e.get('artifacts', [])]
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]['name'], 'report.html')
            self.assertIn('not verified', artifacts[0]['evidence'])
            report.unlink()
            after = reader.snapshot()
            self.assertNotEqual(after['version'], snap['version'])
            self.assertFalse([a for e in after['events'] for a in e.get('artifacts', [])])

    def test_artifact_scope_symlinks_directories_and_code_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base/'workspace'
            root.mkdir()
            (base/'secret.html').write_text('private')
            (root/'link.html').symlink_to(base/'secret.html')
            (root/'code.py').write_text('code')
            (root/'dir.html').mkdir()
            os.mkfifo(root/'pipe.html')
            log = root/'log.jsonl'
            log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id':'chat', 'cwd':str(root)}})+'\n'+
                json.dumps(message('[a](../secret.html) [b](link.html) [c](code.py) [d](dir.html) [e](https://example.com/report.html) [f](pipe.html)'))+'\n')
            self.assertFalse([a for e in RolloutReader(log, 'chat').snapshot()['events'] for a in e.get('artifacts', [])])

    def test_artifact_fences_encoded_traversal_parent_links_and_missing_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base/'workspace'
            root.mkdir()
            (base/'outside.html').write_text('private')
            (root/'report.html').write_text('report')
            (root/'parent').symlink_to(base, target_is_directory=True)
            log = root/'log.jsonl'
            text = '```md\n[a](report.html)\n```\n`[a](report.html)` [b](%2e%2e/outside.html) [c](parent/outside.html)'
            def write(cwd):
                log.write_text(json.dumps({'type':'session_meta','payload':{'id':'chat',**cwd}})+'\n'+json.dumps(message(text))+'\n')
            write({'cwd':str(root)})
            self.assertFalse([a for e in RolloutReader(log,'chat').snapshot()['events'] for a in e.get('artifacts',[])])
            text = '[report](report.html)'
            write({})
            self.assertFalse([a for e in RolloutReader(log,'chat').snapshot()['events'] for a in e.get('artifacts',[])])
