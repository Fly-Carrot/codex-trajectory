#!/usr/bin/env python3
"""Serve FICTIONAL demo data through the real broker and UI. Never reads Claude Code logs."""
import json
from pathlib import Path
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'src' / 'claude_trajectory'))
from support import Transcript  # noqa: E402
import launcher  # noqa: E402


def main():
    with tempfile.TemporaryDirectory(prefix='claude-trajectory-demo-') as temp:
        root = Path(temp).resolve()
        projects, workspace = root / 'projects', root / 'workspace'
        workspace.mkdir()
        (workspace / 'review.md').write_text('# Fictional review\n')
        session = 'demo-session'
        t = Transcript(projects / '-demo-workspace' / (session + '.jsonl'), session, cwd=str(workspace))
        t.queue(0)
        t.prompt(1, 'Add a compact result inspector and check the mobile layout.')
        t.text(3, 'I will inspect the layout, make a focused patch, and test it.', message_id='m1')
        t.call(4, 'toolu_plan', 'TodoWrite', {'todos': [{'content': 'Inspect layout', 'status': 'in_progress'}]}, message_id='m1', index=1)
        t.result(4, 'toolu_plan', 'Todos updated')
        t.call(5, 'toolu_grep', 'Grep', {'pattern': 'inspector', 'path': 'src'}, message_id='m2')
        t.result(6, 'toolu_grep', 'src/viewer.html:84: selected event panel')
        t.call(7, 'toolu_read', 'Read', {'file_path': str(workspace / 'src/viewer.html')}, message_id='m3')
        t.result(7, 'toolu_read', '84  <aside id="inspector">')
        t.call(8, 'toolu_agent', 'Agent', {'subagent_type': 'Explore', 'description': 'Review keyboard navigation', 'prompt': 'Read-only review.'}, message_id='m4')
        t.result(9, 'toolu_agent', 'Async agent launched', tool_result={'isAsync': True, 'status': 'async_launched', 'agentId': 'demoagent1'})
        t.call(10, 'toolu_edit', 'Edit', {'file_path': str(workspace / 'src/viewer.html'), 'old_string': 'a', 'new_string': 'b'}, message_id='m5')
        t.result(11, 'toolu_edit', 'The file has been updated.')
        t.call(12, 'toolu_mcp', 'mcp__browser__check_mobile', {'width': 390}, message_id='m6')
        t.result(14, 'toolu_mcp', '390px viewport: labels readable; inspector does not overlap the timeline.')
        t.call(15, 'toolu_test', 'Bash', {'command': 'python3 -m unittest discover -s tests', 'description': 'Run tests'}, message_id='m7')
        t.result(18, 'toolu_test', 'Ran 59 tests. OK.')
        t.call(19, 'toolu_fail', 'Bash', {'command': 'npm run lint'}, message_id='m8')
        t.result(20, 'toolu_fail', 'Exit code 1\nlint: 1 warning treated as error', is_error=True)
        t.call(21, 'toolu_web', 'WebSearch', {'query': 'aria-pressed toggle button'}, message_id='m9')
        t.result(23, 'toolu_web', 'Fictional search results')
        t.notification(25, 'toolu_agent', summary='Agent "Review keyboard navigation" finished')
        t.handback(25, 'demoagent1', 'Keyboard navigation verified: arrow keys pan, Enter opens details.')
        t.text(27, 'Ready for review. Tests pass and the mobile layout is clean. See [review](review.md).', stop='end_turn', message_id='m10')
        t.write()
        sub = Transcript(projects / '-demo-workspace' / session / 'subagents' / 'agent-demoagent1.jsonl', session,
                         agent='demoagent1', cwd=str(workspace))
        sub.prompt(9, 'Read-only review.')
        sub.call(13, 'toolu_sub', 'Read', {'file_path': str(workspace / 'src/keys.js')}, message_id='s1')
        sub.result(14, 'toolu_sub', 'export function keys() {}')
        sub.text(22, 'Keyboard navigation verified.', stop='end_turn', message_id='s2')
        sub.write()
        (sub.path.parent / 'agent-demoagent1.meta.json').write_text(json.dumps({'agentType': 'Explore', 'toolUseId': 'toolu_agent'}))
        state = root / 'state'
        result = launcher.launch({'hook_event_name': 'Manual', 'session_id': session, 'transcript_path': str(t.path)},
                                 state=state, projects=projects)
        print(result['url'], flush=True)

        def interrupt(signum, frame):
            raise KeyboardInterrupt

        # Background shells may ignore SIGINT; SIGTERM must still clean up.
        signal.signal(signal.SIGTERM, interrupt)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            launcher.stop(state, projects)


if __name__ == '__main__':
    main()
