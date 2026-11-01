"""SYNTHETIC test fixtures shaped like Claude Code 2.1.185 transcripts.

Every record here is invented. These fixtures exercise the parser; they are not a
substitute for acceptance against a real Claude Code session.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src' / 'claude_trajectory'))

START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


class Transcript:
    """Builds a synthetic main or subagent transcript, one record per line."""

    def __init__(self, path, session, agent=None, cwd='/work/demo'):
        self.path, self.session, self.agent, self.cwd = Path(path), session, agent, cwd
        self.records = []
        self.n = 0

    def base(self, kind, t, **extra):
        self.n += 1
        record = {'type': kind, 'timestamp': stamp(t), 'sessionId': self.session, 'uuid': f'u{self.n:04d}-{abs(hash(str(self.path))) % 10**6}',
                  'cwd': self.cwd, 'isSidechain': bool(self.agent), 'version': '2.1.185'}
        if self.agent:
            record['agentId'] = self.agent
        record.update(extra)
        self.records.append(record)
        return record

    def queue(self, t):
        self.records.append({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': stamp(t), 'sessionId': self.session})

    def prompt(self, t, text, prompt_id='p1', **extra):
        return self.base('user', t, message={'role': 'user', 'content': text}, promptId=prompt_id,
                         **{'origin': {'kind': 'human'}, **extra})

    def assistant(self, t, blocks, stop='tool_use', message_id='msg1', index=0, **extra):
        return self.base('assistant', t, message={'id': message_id, 'role': 'assistant', 'type': 'message',
                                                  'content': blocks, 'stop_reason': stop}, apiBlockIndex=index,
                         requestId='req1', **extra)

    def text(self, t, text, stop='tool_use', message_id='msg1', index=0, **extra):
        return self.assistant(t, [{'type': 'text', 'text': text}], stop, message_id, index, **extra)

    def thinking(self, t, message_id='msg1', index=0):
        return self.assistant(t, [{'type': 'thinking', 'thinking': 'SECRET-REASONING-SHOULD-NEVER-APPEAR', 'signature': 'x'}],
                              'tool_use', message_id, index)

    def call(self, t, call_id, name, data, message_id='msg1', index=1):
        return self.assistant(t, [{'type': 'tool_use', 'id': call_id, 'name': name, 'input': data, 'caller': {'type': 'direct'}}],
                              'tool_use', message_id, index)

    def result(self, t, call_id, content, is_error=None, tool_result=None, prompt_id='p1'):
        block = {'type': 'tool_result', 'tool_use_id': call_id, 'content': content}
        if is_error is not None:
            block['is_error'] = is_error
        return self.base('user', t, message={'role': 'user', 'content': [block]}, promptId=prompt_id,
                         toolUseResult=tool_result if tool_result is not None else {'stdout': content, 'stderr': '', 'interrupted': False})

    def notification(self, t, call_id, status='completed', summary='Agent "probe" finished'):
        prompt = (f'<task-notification>\n<task-id>x</task-id>\n<tool-use-id>{call_id}</tool-use-id>\n'
                  f'<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>')
        return self.base('attachment', t, attachment={'type': 'queued_command', 'prompt': prompt, 'commandMode': 'task-notification',
                                                       'origin': {'kind': 'task-notification', 'producer': 'session-task'}})

    def handback(self, t, agent, report, name='Explore'):
        return self.base('attachment', t, attachment={'type': 'queued_command', 'prompt': 'x', 'commandMode': 'prompt', 'isMeta': True,
                                                       'origin': {'kind': 'peer', 'from': agent, 'name': name, 'handback': True,
                                                                  'handbackReportText': report}})

    def write(self, partial_tail=b''):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'wb') as stream:
            for record in self.records:
                stream.write(json.dumps(record).encode() + b'\n')
            stream.write(partial_tail)
        return self.path

    def append(self, *records, raw=b''):
        with open(self.path, 'ab') as stream:
            for record in records:
                stream.write(json.dumps(record).encode() + b'\n')
            stream.write(raw)


class Workspace:
    """A private temporary Claude home with a projects directory and observer state."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-trajectory-test-')
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'claude'
        self.projects = self.home / 'projects'
        self.projects.mkdir(parents=True)
        self.state = self.root / 'state'
        self.cwd = self.root / 'work'
        self.cwd.mkdir()

    def transcript(self, session, project='-work-demo'):
        return Transcript(self.projects / project / (session + '.jsonl'), session, cwd=str(self.cwd))

    def subagent(self, session, agent, call_id, agent_type='Explore', project='-work-demo', meta=True):
        directory = self.projects / project / session / 'subagents'
        directory.mkdir(parents=True, exist_ok=True)
        if meta:
            (directory / f'agent-{agent}.meta.json').write_text(json.dumps(
                {'agentType': agent_type, 'toolUseId': call_id, 'description': 'synthetic', 'finished': True}))
        return Transcript(directory / f'agent-{agent}.jsonl', session, agent=agent, cwd=str(self.cwd))

    def cleanup(self):
        self.temp.cleanup()


def payload(event, session, path):
    return {'hook_event_name': event, 'session_id': session, 'transcript_path': str(path), 'cwd': '/work/demo'}


def node():
    return os.environ.get('TRAJECTORY_NODE') or __import__('shutil').which('node')
