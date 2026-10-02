import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.trace_viewer import server as viewer


def record(kind, **fields):
    return {"type": "response_item", "timestamp": "2026-10-02T13:00:00Z", "payload": {"type": kind, **fields}}


def completed(kind, **fields):
    return {"type": "event_msg", "timestamp": "2026-10-02T13:00:01Z", "payload": {
        "type": "item_completed", "thread_id": "thread-a", "turn_id": "turn-1",
        "started_at_ms": 1790946000000, "completed_at_ms": 1790946001000,
        "item": {"type": kind, **fields}}}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.header = json.dumps({"type": "session_meta", "payload": {"id": "thread-a"}}) + "\n"
        self.path.write_text(self.header)
        self.reader = viewer.RolloutReader(self.path, "thread-a")

    def append(self, value):
        with self.path.open("a") as f:
            f.write(json.dumps(value) + "\n")

    def test_partial_line_then_append_once(self):
        self.reader.snapshot()
        raw = json.dumps(record("custom_tool_call", name="exec", input="echo hello", call_id="c1")).encode()
        with self.path.open("ab") as f:
            f.write(raw[:35])
        self.assertEqual(self.reader.snapshot()["events"], [])
        with self.path.open("ab") as f:
            f.write(raw[35:] + b"\n")
        data = self.reader.snapshot()
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data, self.reader.snapshot())

    def test_no_reasoning_or_system_messages(self):
        for r in [record("reasoning", summary="private-reason"),
                  record("message", role="system", content=[{"type": "input_text", "text": "system-secret"}]),
                  record("message", role="assistant", channel="analysis", content=[{"type": "output_text", "text": "hidden-analysis"}])]:
            self.append(r)
        self.append(record("message", role="assistant", phase="commentary", content=[{"type": "output_text", "text": "public progress"}]))
        data = self.reader.snapshot()
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["detail"], "public progress")

    def test_array_outputs_and_call_ids(self):
        self.append(record("custom_tool_call_output", call_id="c1", output=[{"type": "text", "text": "result"}, {"type": "image", "data": "PRIVATE_IMAGE"}]))
        e = self.reader.snapshot()["events"][0]
        self.assertEqual(e["call_id"], "c1")
        self.assertEqual(e["state"], "returned")
        self.assertNotIn("PRIVATE_IMAGE", e["detail"])
        self.assertEqual(e['output_time'], '2026-10-02T13:00:00Z')

    def test_output_timestamp_is_return_time_not_start_time(self):
        self.append(completed('CommandExecution', id='c1', command='echo done', stdout='done'))
        event = self.reader.snapshot()['events'][0]
        self.assertNotEqual(event['time'], event['output_time'])
        self.assertTrue(event['output_time'].endswith('01+00:00'))

    def test_started_output_does_not_invent_a_return(self):
        value = completed('CommandExecution', id='c1', command='echo hi', stdout='partial')
        value['payload']['type'] = 'item_started'
        self.append(value)
        self.assertNotIn('output_time', self.reader.snapshot()['events'][0])

    def test_wrong_thread_rejected(self):
        with self.assertRaises(ValueError):
            viewer.RolloutReader(self.path, "thread-b")

    def test_truncation_and_replacement_revalidate(self):
        self.append(record("custom_tool_call", name="old", input="x"))
        self.reader.snapshot()
        self.path.write_text(self.header)
        self.assertEqual(self.reader.snapshot()["events"], [])
        self.path.write_text('{"type":"session_meta","payload":{"id":"other"}}\n')
        self.reader.identity = None
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    def test_bounded_events_and_malformed_rows(self):
        with self.path.open("a") as f:
            f.write("broken-json\n")
        for i in range(180):
            self.append(record("function_call", name="test", arguments=str(i)))
        data = self.reader.snapshot()
        self.assertEqual(len(data["events"]), 160)
        self.assertEqual(data["skipped"], 1)

    def test_oversized_line_recovers(self):
        self.reader.snapshot()
        with self.path.open("ab") as f:
            f.write(b"x" * (viewer.MAX_LINE + 5))
        self.reader.snapshot()
        with self.path.open("ab") as f:
            f.write(b"end\n")
        self.append(record("function_call", name="recovered", arguments="ok"))
        self.assertEqual(self.reader.snapshot()["events"][0]["title"], "recovered")

    def test_source_is_never_modified(self):
        self.append(record("function_call", name="test", arguments="ok"))
        before = self.path.read_bytes()
        self.reader.snapshot()
        self.reader.snapshot()
        self.assertEqual(before, self.path.read_bytes())

    def test_structured_public_items_are_not_dropped(self):
        cases = [('CommandExecution', 'COMMAND', {'command': 'echo hello', 'exit_code': 0}),
                 ('FileChange', 'FILE', {'changes': {'src/a.py': {'type': 'update'}}}),
                 ('McpToolCall', 'MCP', {'server': 'browser', 'tool': 'read'}),
                 ('CollabAgentToolCall', 'AGENT', {'tool': 'spawn_agent', 'receiver_thread_ids': ['child-1']}),
                 ('ContextCompaction', 'CONTEXT', {})]
        for kind, tag, fields in cases:
            self.append(completed(kind, id=kind, status='completed', **fields))
        events = self.reader.snapshot()['events']
        self.assertEqual([e['category'] for e in events], [c[1] for c in cases])
        self.assertEqual(events[0]['duration_ms'], 1000)

    def test_call_result_and_structured_completion_are_merged_by_id(self):
        self.append(record('function_call', id='fc-1', name='exec_command', call_id='c1', arguments='echo hello'))
        self.append(record('function_call_output', call_id='c1', output='hello'))
        self.append(completed('CommandExecution', id='c1', command='echo hello', status='completed', exit_code=0, stdout='hello'))
        events = self.reader.snapshot()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['category'], 'COMMAND')
        self.assertEqual(events[0]['output'], 'hello')
        self.assertEqual(events[0]['duration_ms'], 1000)

    def test_structured_reasoning_never_exposed(self):
        self.append(completed('Reasoning', id='r1', raw_content='HIDDEN', summary_text='PRIVATE'))
        self.assertEqual(self.reader.snapshot()['events'], [])

    def test_same_text_with_different_call_ids_is_not_merged(self):
        for i in range(2):
            self.append(completed('CommandExecution', id='c'+str(i), command='echo hello', status='completed'))
        self.assertEqual(len(self.reader.snapshot()['events']), 2)

    def test_same_inode_larger_foreign_session_is_rejected(self):
        self.reader.snapshot()
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'other'}}) + '\n' +
                             json.dumps(record('message', role='user', content='OTHER CHAT ' * 100)) + '\n')
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    def test_partial_reasoning_is_not_kept_in_reader_memory(self):
        with self.path.open('ab') as f:
            f.write(b'{"type":"response_item","payload":{"type":"reasoning","content":"PRIVATE')
        self.reader.snapshot()
        self.assertEqual(self.reader.pending, b'')

    def test_started_item_cannot_regress_known_completion(self):
        self.append(completed('CommandExecution', id='c1', status='completed', command='echo ok'))
        start = completed('CommandExecution', id='c1', status='running', command='echo ok')
        start['payload']['type'] = 'item_started'
        self.append(start)
        self.assertEqual(self.reader.snapshot()['events'][0]['state'], 'completed')

    def test_raw_output_cannot_overwrite_structured_result(self):
        self.append(completed('CommandExecution', id='c1', command='false', exit_code=1, stdout='real failure'))
        self.append(record('function_call_output', call_id='c1', output='wrapper says success'))
        event = self.reader.snapshot()['events'][0]
        self.assertEqual(event['output'], 'real failure')
        self.assertEqual(event['state'], 'failed')

    def test_structured_failure_and_message_mirror(self):
        self.append(record('message', id='msg-1', role='assistant', phase='commentary', content='Public note'))
        self.append(completed('AgentMessage', id='msg-1', phase='commentary', content=[{'type':'Text','text':'Public note'}]))
        self.append(completed('CommandExecution', id='c1', command='false', status='completed', exit_code=1))
        events = self.reader.snapshot()['events']
        self.assertEqual(len(events), 2)
        self.assertEqual(next(e for e in events if e['category']=='COMMAND')['state'], 'failed')

    def test_mcp_binary_and_unknown_internal_fields_are_not_rendered(self):
        self.append(completed('McpToolCall', id='m1', server='test', tool='test', status='completed', result={
            'content':[{'type':'text','text':'public'},{'type':'image','data':'PRIVATE_IMAGE'}],
            'isError':True, 'raw_content':'PRIVATE_REASONING'}))
        self.append(completed('UnknownInternalItem', id='x1', content='PRIVATE_UNKNOWN'))
        events = self.reader.snapshot()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['output'], 'public')
        self.assertEqual(events[0]['state'], 'failed')

    def test_wrong_structured_thread_and_invalid_timestamp(self):
        other = completed('CommandExecution', id='c1', command='foreign')
        other['payload']['thread_id'] = 'other'
        self.append(other)
        bad_time = record('function_call', name='valid', arguments='public')
        bad_time['timestamp'] = {'malformed':True}
        self.append(bad_time)
        self.assertEqual(self.reader.snapshot()['events'][0]['title'], 'valid')


class SecurityTests(unittest.TestCase):
    def test_compact_english_scroll_contract(self):
        html = Path(viewer.__file__).with_name('index.html').read_text()
        self.assertIn('<html lang="en">', html)
        self.assertIn('id="timeline-scroll"', html)
        self.assertIn('id="inspector"', html)
        self.assertNotIn('class="stats"', html)

    def test_generated_event_labels_are_english(self):
        value = viewer.normalize(record('message', role='user', content='hello'), 0)
        self.assertEqual(value['title'], 'User')

    def test_token_counts_are_not_credentials(self):
        value = '{"max_output_tokens":1200,"original_token_count":42,"access_token":"private-value"}'
        result = viewer.redact(value)
        self.assertIn('"max_output_tokens":1200', result)
        self.assertIn('"original_token_count":42', result)
        self.assertNotIn('private-value', result)

    def test_redaction(self):
        value = 'API_KEY="abc123" password=abc456 Bearer ABCD.efgh sk-abcdefghijklmnop /Users/example/private'
        result = viewer.redact(value)
        for secret in ("abc123", "abc456", "ABCD.efgh", "abcdefghijklmnop", "/Users/example"):
            self.assertNotIn(secret, result)

    def test_http_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.jsonl"
            path.write_text('{"type":"session_meta","payload":{"id":"a"}}\n')
            server = viewer.ViewerServer(viewer.RolloutReader(path, "a"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(server.origin + "/api/events")
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                headers = {"Authorization": "Bearer " + server.token}
                with urlopen(Request(server.origin + "/api/events", headers=headers)) as response:
                    self.assertEqual(json.load(response)["thread_id"], "a")
                for bad in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(Request(server.origin + "/api/events", headers={**headers, **bad}))
                    self.assertEqual(denied.exception.code, 403)
                    denied.exception.close()
                with self.assertRaises(HTTPError) as denied:
                    urlopen(server.origin + "/../../source.jsonl")
                denied.exception.close()
                self.assertNotIn(b"innerHTML", server.html)
                self.assertIn(b"textContent", server.html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
