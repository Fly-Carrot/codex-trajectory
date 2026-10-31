"""Event classification on SYNTHETIC records shaped like Claude Code 2.1.185."""
import json
import unittest

import support  # noqa: F401  (adds the source directory to sys.path)
from support import Transcript
from events import normalize, merge_event, redact


def build():
    return Transcript('/tmp/never-written/s1.jsonl', 'session-1')


def only(record, context=None):
    events = normalize(record, context or {'turn': 'p1'})
    return events


class UserAndAgentTests(unittest.TestCase):
    def test_prompt_has_turn_boundary_and_user_event(self):
        t = build()
        events = only(t.prompt(1, 'Please check the layout', prompt_id='p9'))
        self.assertEqual([e['category'] for e in events], ['TURN', 'USER'])
        self.assertEqual(events[0]['id'], 'boundary:p9:start')
        self.assertEqual(events[1]['turn_id'], 'p9')
        self.assertEqual(events[1]['lane'], 'input')

    def test_injected_and_non_human_user_records_are_not_user_input(self):
        t = build()
        self.assertEqual(only(t.prompt(1, 'skill body', isMeta=True)), [])
        self.assertEqual(only(t.prompt(1, 'summary', isCompactSummary=True)), [])
        self.assertEqual(only(t.prompt(1, '<task-notification>x</task-notification>', origin={'kind': 'task-notification'})), [])
        self.assertEqual(only(t.prompt(1, '<local-command-stdout>ok</local-command-stdout>')), [])

    def test_slash_command_and_interrupt(self):
        t = build()
        self.assertEqual(only(t.prompt(1, '<command-name>/review</command-name><command-args>x</command-args>'))[1]['title'], 'User command /review')
        interrupt = only(t.prompt(2, '[Request interrupted by user]'))
        self.assertEqual(interrupt[0]['state'], 'turn_aborted')
        self.assertEqual(interrupt[0]['lane'], 'boundary')

    def test_reply_phase_follows_recorded_stop_reason_only(self):
        t = build()
        self.assertEqual(only(t.text(1, 'Working on it', stop='tool_use'))[0]['reply_phase'], 'Progress')
        final = only(t.text(2, 'Done.', stop='end_turn', message_id='m2'))[0]
        self.assertEqual(final['reply_phase'], 'Final')
        self.assertEqual(final['category'], 'ASSISTANT')
        self.assertNotIn('reply_phase', only(t.text(3, 'Streaming', stop=None, message_id='m3'))[0])

    def test_api_error_message_is_marked_error(self):
        t = build()
        event = only(t.text(1, 'Not logged in', stop='stop_sequence', isApiErrorMessage=True))[0]
        self.assertEqual((event['state'], event['title']), ('error', 'API error'))

    def test_thinking_and_raw_requests_never_produce_events(self):
        t = build()
        self.assertEqual(only(t.thinking(1)), [])
        raw = {'type': 'api-request', 'sessionId': 'session-1', 'params': {'model': 'x'}, 'tail': 'SECRET'}
        blob = {'type': 'api-request-blob', 'sessionId': 'session-1', 'message': {'content': [{'type': 'text', 'text': 'SECRET'}]}}
        for record in (raw, blob, {'type': 'file-history-snapshot'}, {'type': 'last-prompt', 'lastPrompt': 'SECRET'}):
            self.assertEqual(normalize(record), [])
        mixed = t.assistant(2, [{'type': 'thinking', 'thinking': 'SECRET-THOUGHT'}, {'type': 'text', 'text': 'Visible'}], message_id='m9')
        events = only(mixed)
        self.assertEqual(len(events), 1)
        self.assertNotIn('SECRET', json.dumps(events))

    def test_text_block_identity_is_stable_for_rerecorded_blocks(self):
        t = build()
        first = only(t.text(1, 'Same block', message_id='mX', index=2))[0]
        again = only(t.text(1, 'Same block', message_id='mX', index=2))[0]
        self.assertEqual(first['id'], again['id'])


class ToolTests(unittest.TestCase):
    def call(self, name, data, **result):
        t = build()
        call = only(t.call(1, 'toolu_A', name, data))[0]
        if not result:
            return call, None
        returned = only(t.result(2, 'toolu_A', **result))[0]
        return call, merge_event(call, returned)

    def test_command_success_failure_and_exit_code(self):
        call, done = self.call('Bash', {'command': 'ls -la\necho x', 'description': 'List'}, content='ok')
        self.assertEqual((call['category'], call['state'], call['title']), ('COMMAND', 'running', 'ls -la'))
        self.assertEqual((done['state'], done['output']), ('returned', 'ok'))
        _, failed = self.call('Bash', {'command': 'false'}, content='Exit code 1\nboom', is_error=True)
        self.assertEqual((failed['state'], failed['exit_code']), ('failed', 1))

    def test_pending_result_is_not_failure(self):
        call, _ = self.call('Bash', {'command': 'sleep 9'})
        self.assertEqual(call['state'], 'running')

    def test_interrupted_result(self):
        _, done = self.call('Bash', {'command': 'x'}, content='', tool_result={'interrupted': True})
        self.assertEqual(done['state'], 'interrupted')

    def test_file_actions_are_distinct(self):
        for name, action in (('Read', 'read'), ('Edit', 'edit'), ('Write', 'write'), ('NotebookEdit', 'edit')):
            event, _ = self.call(name, {'file_path': '/work/demo/src/a.py'})
            self.assertEqual((event['category'], event['file_action']), ('FILE', action))
            self.assertEqual(event['title'], action.capitalize() + ' src/a.py')

    def test_skill_file_read_is_not_skill_execution(self):
        event, _ = self.call('Read', {'file_path': '/x/skills/design/SKILL.md'})
        self.assertEqual(event['badges'], ['Skill file read: design'])
        self.assertNotIn('invoked', json.dumps(event))

    def test_skill_tool_is_invoked_not_completed_and_plugin_needs_namespace(self):
        _, done = self.call('Skill', {'skill': 'data:analyze'}, content='Launching skill')
        self.assertEqual(done['badges'], ['Skill: data:analyze (invoked)', 'Plugin: data'])
        self.assertNotIn('completed', json.dumps(done['badges']))
        plain, _ = self.call('Skill', {'skill': 'review'})
        self.assertEqual(plain['badges'], ['Skill: review (invoked)'])

    def test_mcp_names_and_plugin_badge(self):
        event, _ = self.call('mcp__zotero__zotero_search_items', {'q': 'x'})
        self.assertEqual((event['category'], event['title']), ('MCP', 'zotero.zotero_search_items'))
        self.assertEqual(event['badges'], [])
        plugin, _ = self.call('mcp__plugin_data_hex__run', {})
        self.assertEqual(plugin['badges'], ['Plugin MCP'])

    def test_only_native_search_tools_are_search(self):
        for name in ('WebSearch', 'Grep', 'Glob'):
            self.assertEqual(self.call(name, {'query': 'q', 'pattern': 'p'})[0]['category'], 'SEARCH')
        self.assertEqual(self.call('WebFetch', {'url': 'https://example.org'})[0]['category'], 'TOOL')
        self.assertEqual(self.call('mcp__Claude_Browser__navigate', {'url': 'x'})[0]['category'], 'MCP')
        self.assertEqual(self.call('Bash', {'command': 'curl https://example.org'})[0]['category'], 'COMMAND')

    def test_plan_operations_are_distinct_and_never_labelled_approved(self):
        enter = self.call('EnterPlanMode', {})[0]
        propose, decided = self.call('ExitPlanMode', {'plan': '1. Do x'}, content='User has approved your plan')
        tasks = self.call('TodoWrite', {'todos': []})[0]
        self.assertEqual({enter['title'], propose['title'], tasks['title']},
                         {'Enter plan mode (requested)', 'Plan proposed', 'Task list update'})
        self.assertEqual(decided['title'], 'Plan proposed')
        self.assertEqual(decided['state'], 'returned')
        self.assertNotIn('approved', decided['title'].lower())

    def test_unknown_tool_goes_to_other_tools(self):
        self.assertEqual(self.call('ToolSearch', {'query': 'x'})[0]['category'], 'TOOL')

    def test_recorded_duration_only(self):
        _, done = self.call('Bash', {'command': 'x'}, content='ok', tool_result={'stdout': 'ok'})
        self.assertIsNone(done['duration_ms'])
        _, timed = self.call('Agent', {'subagent_type': 'Explore', 'prompt': 'x'}, content='done',
                             tool_result={'status': 'completed', 'totalDurationMs': 1234})
        self.assertEqual(timed['duration_ms'], 1234)


class SubagentTests(unittest.TestCase):
    def test_async_dispatch_result_and_notification_merge_by_call_id(self):
        t = build()
        call = only(t.call(1, 'toolu_D', 'Agent', {'subagent_type': 'Explore', 'description': 'probe', 'prompt': 'look'}))[0]
        launched = only(t.result(2, 'toolu_D', 'Async agent launched', tool_result={'isAsync': True, 'status': 'async_launched', 'agentId': 'a123'}))[0]
        merged = merge_event(call, launched)
        self.assertEqual((merged['category'], merged['state'], merged['agent_id']), ('AGENT', 'launched', 'a123'))
        done = only(t.notification(9, 'toolu_D'))[0]
        merged = merge_event(merged, done)
        self.assertEqual((merged['state'], merged['title'], merged['time']), ('completed', 'Dispatch Explore: probe', call['time']))
        self.assertEqual(merged['completion_time'], done['time'])

    def test_subagent_events_stay_in_subagent_lane(self):
        t = Transcript('/tmp/x/agent-a1.jsonl', 'session-1', agent='a1')
        context = {'agent': 'a1', 'agent_type': 'Explore'}
        bash = normalize(t.call(1, 'toolu_S', 'Bash', {'command': 'ls'}), context)[0]
        reply = normalize(t.text(2, 'Found it', stop='end_turn', message_id='mS'), context)[0]
        for event in (bash, reply):
            self.assertEqual((event['lane'], event['category'], event['agent_id']), ('subagent', 'AGENT', 'a1'))
        self.assertNotIn('reply_phase', reply)
        self.assertEqual(normalize(t.prompt(0, 'Task prompt'), context), [])

    def test_handback_is_linked_by_sender_agent_id(self):
        event = only(build().handback(5, 'a123', 'Report text'))[0]
        self.assertEqual((event['lane'], event['agent_id'], event['detail']), ('subagent', 'a123', 'Report text'))

    def test_legacy_inline_sidechain_is_not_guessed(self):
        record = build().text(1, 'inline', isSidechain=True)
        self.assertEqual(normalize(record, {}), [])


class ContextAndRedactionTests(unittest.TestCase):
    def test_compaction_boundary(self):
        record = {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'c1', 'timestamp': '2026-10-01T09:00:00Z',
                  'sessionId': 'session-1', 'compactMetadata': {'trigger': 'auto', 'preTokens': 1}}
        event = normalize(record)[0]
        self.assertEqual((event['category'], event['title']), ('CONTEXT', 'Context compacted'))
        self.assertIn('auto', event['detail'])
        self.assertEqual(normalize({**record, 'subtype': 'stop_hook_summary'}), [])

    def test_redaction(self):
        text = redact('token=abc123 Bearer XYZ sk-ant-abcdefghijk /Users/alice/x http://127.0.0.1:5/s/a#secrettoken')
        for secret in ('abc123', 'XYZ', 'abcdefghijk', 'alice', 'secrettoken'):
            self.assertNotIn(secret, text)

    def test_oversized_output_is_bounded(self):
        t = build()
        event = only(t.result(1, 'toolu_Z', 'x' * 500000))[0]
        self.assertLess(len(event['output']), 3000)
        self.assertTrue(event['output'].endswith('[preview truncated]'))

    def test_markup_is_data(self):
        t = build()
        event = only(t.prompt(1, '<img src=x onerror=alert(1)><script>alert(2)</script>'))[1]
        self.assertIn('<script>', event['detail'])


if __name__ == '__main__':
    unittest.main()
