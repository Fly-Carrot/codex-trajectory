"""Allowlisted public Claude Code transcript events. Never render arbitrary records.

Redaction helpers are adapted from codex-trajectory (MIT, Copyright (c) 2026 Fly-Carrot).
"""

from datetime import datetime
import json
import math
import re


SECRET_NAME = r'(?:[\w-]{0,64}(?:password|secret|api[_-]?key)[\w-]{0,64}|(?:[\w-]{1,64}[_-])?token)'
SECRET_KEY = re.compile(SECRET_NAME, re.I)
SECRET_ASSIGNMENT = re.compile(r'\b' + SECRET_NAME + r'''(?:\\*["'])?\s*[:=]\s*''', re.I)
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}')
TOOL_ID = re.compile(r'[a-zA-Z0-9_-]{1,128}')

COMMAND_TOOLS = {'Bash', 'BashOutput', 'KillShell', 'KillBash', 'PowerShell'}
FILE_TOOLS = {'Read': 'read', 'NotebookRead': 'read', 'Write': 'write', 'Edit': 'edit',
              'MultiEdit': 'edit', 'NotebookEdit': 'edit'}
SEARCH_TOOLS = {'WebSearch', 'Grep', 'Glob'}
AGENT_TOOLS = {'Agent', 'Task'}
PLAN_TOOLS = {'EnterPlanMode': 'Enter plan mode (requested)', 'ExitPlanMode': 'Plan proposed',
              'TodoWrite': 'Task list update', 'TaskCreate': 'Task list: create',
              'TaskUpdate': 'Task list: update'}
FAILED_STATUS = {'failed': 'failed', 'error': 'failed', 'killed': 'cancelled', 'stopped': 'cancelled',
                 'cancelled': 'cancelled', 'completed': 'completed'}


def public_text(value):
    if isinstance(value, list):
        return "\n".join(filter(None, (public_text(v) for v in value)))
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        return ""
    return value if isinstance(value, str) else ""


def credential_end(text, start):
    end, delimiter = start, ''
    # Adjacent quoted/unquoted shell pieces belong to the same credential.
    while end < len(text) and not (text[end].isspace() or text[end] in ',;}]&|<>'):
        quote_at = end
        while quote_at < len(text) and text[quote_at] == '\\':
            quote_at += 1
        if quote_at < len(text) and text[quote_at] in ('"', "'"):
            if end == start:
                delimiter = text[end:quote_at + 1]
            escapes = quote_at - end
            slashes = 0
            for index in range(quote_at + 1, len(text)):
                char = text[index]
                # JSON escaping doubles backslashes at each nesting level.
                literal_single_quote = text[quote_at] == "'" and escapes == 0
                if char == text[quote_at] and (literal_single_quote or slashes % (2 * (escapes + 1)) == escapes):
                    end = index + 1
                    break
                slashes = slashes + 1 if char == '\\' else 0
            else:
                return len(text), delimiter
        elif text.startswith('[redacted]', end):
            end += len('[redacted]')
        elif text[end] in '[{':
            # An incomplete JSON credential container has no safe boundary.
            return len(text), delimiter
        else:
            while end < len(text) and not (text[end].isspace() or text[end] in '\"\',;}]&|<>'):
                end += 2 if text[end] == '\\' and end + 1 < len(text) else 1
    return end, delimiter


def redact_text(text):
    text = re.sub(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", text)
    text = re.sub(r"\b(?:sk-|sk-ant-|ghp_|gho_|github_pat_|nvapi-)[A-Za-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(r"(http://127\.0\.0\.1:\d+/[^\s#]*#)[A-Za-z0-9_-]+", r"\1[redacted]", text)
    parts, end = [], 0
    for match in SECRET_ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        parts.append(text[end:match.end()])
        end, delimiter = credential_end(text, match.end())
        parts.append(delimiter + '[redacted]' + delimiter)
    text = ''.join(parts) + text[end:]
    text = re.sub(r"/(?:Users|home)/[^/\s]+", "~", text)
    return text


def redact_structure(value, depth=0):
    # Called only on already selected public fields, never on a whole record.
    if depth > 16:
        return '[preview truncated]'
    if isinstance(value, dict):
        return {redact(str(key)): '[redacted]' if SECRET_KEY.fullmatch(str(key))
                else redact_structure(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(item, depth + 1) for item in value]
    return redact(value) if isinstance(value, str) else value


def redact(text):
    text = str(text)
    oversized = len(text) > 8192
    # Bound parsing and regex input before scanning binary-like text.
    text = text[:8192]
    structured = None
    if text.lstrip().startswith(('{', '[')):
        try:
            structured = json.loads(text)
        except (ValueError, RecursionError):
            pass
    if isinstance(structured, (dict, list)):
        text = json.dumps(redact_structure(structured), ensure_ascii=False, separators=(',', ':'))
    else:
        text = redact_text(text)
    return text[:2400] + ("\n[preview truncated]" if oversized or len(text) > 2400 else "")


def preview(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return redact(value)
    text = json.dumps(redact_structure(value), ensure_ascii=False)
    return text[:2400] + ("\n[preview truncated]" if len(text) > 2400 else "")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(record):
    stamp = record.get('timestamp')
    if not isinstance(stamp, str):
        return ''
    try:
        datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        return ''
    return stamp


def short_path(path, cwd):
    if not isinstance(path, str):
        return ''
    if isinstance(cwd, str) and cwd and path.startswith(cwd.rstrip('/') + '/'):
        path = path[len(cwd.rstrip('/')) + 1:]
    return redact(path)


def mcp_name(name):
    server, _, tool = name[5:].partition('__')
    return server, tool


def recorded_duration(result):
    # Only explicit operation durations; never timestamp gaps or model latency.
    if not isinstance(result, dict):
        return None
    for key, scale in (('durationMs', 1), ('totalDurationMs', 1), ('durationSeconds', 1000)):
        value = result.get(key)
        if number(value) and value >= 0:
            return round(value * scale, 3)
    return None


def base_event(record, ident, context):
    agent = context.get('agent')
    return {'id': ident, 'time': timestamp(record), 'lane': 'subagent' if agent else 'activity',
            'category': 'AGENT' if agent else 'ASSISTANT', 'title': '', 'detail': '', 'call_id': '',
            'state': 'recorded', 'input': '', 'output': '', 'turn_id': '' if agent else context.get('turn', ''),
            'duration_ms': None, 'badges': [], 'part': 'record',
            'source': 'subagent transcript' if agent else 'transcript',
            **({'agent_id': agent, 'agent_type': context.get('agent_type') or ''} if agent else {})}


def subagent_view(event, context):
    """Subagent work stays in the Subagents row, separate from main-session rows."""
    if not context.get('agent'):
        return event
    label = context.get('agent_type') or 'Subagent'
    original = event['category']
    event['lane'], event['category'] = 'subagent', 'AGENT'
    if original != 'ASSISTANT' and event['part'] != 'result':
        event['badges'] = [{'COMMAND': 'Command', 'FILE': 'File', 'MCP': 'MCP', 'SEARCH': 'Search',
                            'PLAN': 'Plan', 'AGENT': 'Dispatch', 'TOOL': 'Tool'}.get(original, 'Tool')] + event['badges']
    event['badges'] = ['Subagent: ' + redact(label)] + event['badges']
    if event['part'] != 'result':
        event['title'] = redact(label) + ' · ' + event['title']
    return event


def tool_call(record, block, context):
    name = block.get('name')
    call = block.get('id')
    if not isinstance(name, str) or not isinstance(call, str) or not TOOL_ID.fullmatch(call):
        return None
    data = block.get('input') if isinstance(block.get('input'), dict) else {}
    cwd = record.get('cwd')
    event = base_event(record, 'call:' + call, context)
    event.update(lane='tools', category='TOOL', call_id=call, state='running', part='call',
                 title=redact(name)[:200], input=preview(data), tool=redact(name)[:120])
    if name in COMMAND_TOOLS:
        command = data.get('command') if isinstance(data.get('command'), str) else ''
        event.update(category='COMMAND', title=redact(command.strip().splitlines()[0] if command.strip() else name)[:300])
    elif name in FILE_TOOLS:
        action = FILE_TOOLS[name]
        path = data.get('file_path') or data.get('notebook_path') or ''
        event.update(category='FILE', file_action=action,
                     title=action.capitalize() + ' ' + (short_path(path, cwd) or 'file'))
        match = re.search(r'/skills/(?:\.system/)?([^/]+)/SKILL\.md$', path) if isinstance(path, str) else None
        if action == 'read' and match:
            event['badges'] = ['Skill file read: ' + redact(match[1])]
    elif name.startswith('mcp__') and name.count('__') >= 2:
        server, tool = mcp_name(name)
        event.update(category='MCP', title=redact(server + '.' + tool)[:200], mcp_server=redact(server))
        if server.startswith('plugin_'):
            event['badges'] = ['Plugin MCP']
    elif name in SEARCH_TOOLS:
        query = data.get('query') if name == 'WebSearch' else data.get('pattern')
        event.update(category='SEARCH', title=('Web search' if name == 'WebSearch' else name) +
                     (': ' + redact(query)[:200] if isinstance(query, str) else ''))
    elif name in AGENT_TOOLS:
        kind = data.get('subagent_type') if isinstance(data.get('subagent_type'), str) else 'general-purpose'
        description = data.get('description') if isinstance(data.get('description'), str) else ''
        event.update(category='AGENT', title='Dispatch ' + redact(kind)[:80] + (': ' + redact(description)[:160] if description else ''),
                     agent_type=redact(kind)[:80], input=preview(data.get('prompt')))
    elif name in PLAN_TOOLS:
        event.update(category='PLAN', title=PLAN_TOOLS[name],
                     input=preview(data.get('plan')) if name == 'ExitPlanMode' else event['input'])
    elif name == 'Skill':
        skill = data.get('skill') if isinstance(data.get('skill'), str) else ''
        if re.fullmatch(r'[\w.@/:-]{1,120}', skill):
            event['title'] = 'Skill ' + redact(skill)
            event['badges'] = ['Skill: ' + redact(skill) + ' (invoked)']
            plugin, _, _ = skill.rpartition(':')
            if plugin:
                event['badges'].append('Plugin: ' + redact(plugin))
    event['detail'] = event['input'] or event['title']
    return subagent_view(event, context)


def tool_result(record, block, context):
    call = block.get('tool_use_id')
    if not isinstance(call, str) or not TOOL_ID.fullmatch(call):
        return None
    result = record.get('toolUseResult')
    event = base_event(record, 'call:' + call, context)
    output = public_text(block.get('content'))
    event.update(lane='tools', category='TOOL', call_id=call, part='result', title='Tool result',
                 state='failed' if block.get('is_error') is True else 'returned',
                 output=redact(output) if output else ('[non-text result omitted]' if block.get('content') else ''),
                 output_time=event['time'], duration_ms=recorded_duration(result))
    if isinstance(result, dict):
        if result.get('interrupted') is True:
            event['state'] = 'interrupted'
        status = result.get('status')
        if result.get('isAsync') is True and status == 'async_launched':
            event['state'] = 'launched'
        elif isinstance(status, str) and status in FAILED_STATUS and event['state'] != 'failed':
            event['state'] = FAILED_STATUS[status]
        agent = result.get('agentId')
        if isinstance(agent, str) and ID.fullmatch(agent) and not context.get('agent'):
            event['agent_id'] = agent
    code = re.match(r'Exit code (\d{1,3})\b', output) if event['state'] == 'failed' else None
    if code:
        event['exit_code'] = int(code[1])
    event['detail'] = event['output']
    return subagent_view(event, context)


def user_events(record, context):
    message = record.get('message') if isinstance(record.get('message'), dict) else {}
    content = message.get('content')
    if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
        return [e for b in content if isinstance(b, dict) and b.get('type') == 'tool_result'
                and (e := tool_result(record, b, context))]
    origin = record.get('origin')
    if (context.get('agent') or record.get('isMeta') or record.get('isCompactSummary')
            or record.get('isVisibleInTranscriptOnly')
            or (origin is not None and not (isinstance(origin, dict) and origin.get('kind') == 'human'))):
        return []
    text = public_text(content).strip()
    if not text or text.startswith(('<local-command-stdout>', '<local-command-stderr>', '<bash-stdout>', '<bash-stderr>')):
        return []
    ident = record.get('uuid') if isinstance(record.get('uuid'), str) and TOOL_ID.fullmatch(record['uuid']) else None
    if not ident:
        return []
    if text.startswith('[Request interrupted by user'):
        event = base_event(record, 'interrupt:' + ident, context)
        event.update(lane='boundary', category='TURN', state='turn_aborted', title='Turn interrupted')
        return [event]
    event = base_event(record, 'item:' + ident, context)
    command = re.match(r'\s*<command-name>/?([\w:.-]{1,80})</command-name>', text)
    event.update(lane='input', category='USER', title='User command /' + command[1] if command else 'User',
                 detail=redact(text))
    events = [event]
    prompt = record.get('promptId')
    if isinstance(prompt, str) and TOOL_ID.fullmatch(prompt):
        boundary = base_event(record, 'boundary:' + prompt + ':start', context)
        boundary.update(lane='boundary', category='TURN', title='Turn started', state='task_started', turn_id=prompt)
        event['turn_id'] = prompt
        events.insert(0, boundary)
    return events


def assistant_events(record, context):
    message = record.get('message') if isinstance(record.get('message'), dict) else {}
    content = message.get('content')
    if not isinstance(content, list):
        return []
    events = []
    message_id = message.get('id') if isinstance(message.get('id'), str) and TOOL_ID.fullmatch(message['id']) else None
    uuid = record.get('uuid') if isinstance(record.get('uuid'), str) and TOOL_ID.fullmatch(record['uuid']) else None
    index = record.get('apiBlockIndex')
    for position, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        # Hidden reasoning and redacted reasoning are never parsed.
        if kind == 'tool_use':
            event = tool_call(record, block, context)
            if event:
                events.append(event)
        elif kind == 'text' and isinstance(block.get('text'), str) and block['text'].strip():
            # One API block may be re-recorded; its message ID and block index identify it.
            if message_id:
                slot = index if len(content) == 1 and type(index) is int else position
                ident = 'text:' + message_id + ':' + str(slot)
            elif uuid:
                ident = 'item:' + uuid + ':' + str(position)
            else:
                continue
            event = base_event(record, ident, context)
            stop = message.get('stop_reason')
            phase = 'Final' if stop == 'end_turn' else 'Progress' if stop == 'tool_use' else None
            event.update(title='Agent reply' if phase != 'Progress' else 'Agent progress', detail=redact(block['text']))
            if phase:
                event['reply_phase'] = phase
            if record.get('isApiErrorMessage') is True:
                event.update(title='API error', state='error', badges=['API error'])
                event.pop('reply_phase', None)
            if context.get('agent'):
                event['badges'] = ['Subagent: ' + redact(context.get('agent_type') or 'Subagent'), 'Subagent reply'] + event['badges']
                event['title'] = redact(context.get('agent_type') or 'Subagent') + ' · reply'
                event.pop('reply_phase', None)
            events.append(event)
    return events


def attachment_events(record, context):
    attachment = record.get('attachment') if isinstance(record.get('attachment'), dict) else {}
    if context.get('agent') or attachment.get('type') != 'queued_command':
        return []
    origin = attachment.get('origin') if isinstance(attachment.get('origin'), dict) else {}
    uuid = record.get('uuid') if isinstance(record.get('uuid'), str) and TOOL_ID.fullmatch(record['uuid']) else None
    if origin.get('kind') == 'task-notification' and isinstance(attachment.get('prompt'), str):
        prompt = attachment['prompt'][:65536]
        call = re.search(r'<tool-use-id>([a-zA-Z0-9_-]{1,128})</tool-use-id>', prompt)
        status = re.search(r'<status>([a-z_]{1,32})</status>', prompt)
        if not call or not status:
            return []
        summary = re.search(r'<summary>([^<]{0,400})</summary>', prompt)
        event = base_event(record, 'call:' + call[1], context)
        state = FAILED_STATUS.get(status[1], 'returned')
        event.update(lane='tools', category='TOOL', call_id=call[1], part='notification', state=state,
                     title='Background task notification', completion_time=event['time'],
                     notification='Background task ' + status[1] + (': ' + redact(summary[1]) if summary else ''))
        event['detail'] = event['notification']
        return [event]
    sender = origin.get('from')
    if (origin.get('kind') == 'peer' and origin.get('handback') is True and isinstance(sender, str)
            and ID.fullmatch(sender) and isinstance(origin.get('handbackReportText'), str) and uuid):
        name = origin.get('name') if isinstance(origin.get('name'), str) else 'Subagent'
        event = base_event(record, 'handback:' + sender + ':' + uuid, context)
        event.update(lane='subagent', category='AGENT', title=redact(name)[:80] + ' · hand-back',
                     detail=redact(origin['handbackReportText']), agent_id=sender, agent_type=redact(name)[:80],
                     badges=['Subagent: ' + redact(name)[:80], 'Hand-back report'])
        return [event]
    return []


def system_events(record, context):
    if record.get('subtype') != 'compact_boundary':
        return []
    uuid = record.get('uuid') if isinstance(record.get('uuid'), str) and TOOL_ID.fullmatch(record['uuid']) else None
    if not uuid:
        return []
    meta = record.get('compactMetadata') if isinstance(record.get('compactMetadata'), dict) else {}
    trigger = meta.get('trigger') if meta.get('trigger') in ('auto', 'manual') else None
    event = base_event(record, 'item:' + uuid, context)
    event.update(lane='activity', category='CONTEXT', title='Context compacted',
                 detail='Compaction recorded' + (' (' + trigger + ')' if trigger else '') + '. Internal context is not displayed.')
    if context.get('agent'):
        return [subagent_view(event, context)]
    return [event]


def normalize(record, context=None):
    """Return the public events in one transcript record. Unknown shapes yield nothing."""
    context = context or {}
    if not isinstance(record, dict):
        return []
    kind = record.get('type')
    if record.get('isSidechain') is True and not context.get('agent'):
        # Legacy inline sidechains have no verified link to a dispatch.
        return []
    if kind == 'user':
        events = user_events(record, context)
    elif kind == 'assistant':
        events = assistant_events(record, context)
    elif kind == 'attachment':
        events = attachment_events(record, context)
    elif kind == 'system':
        events = system_events(record, context)
    else:
        return []
    for event in events:
        event['detail'] = event['detail'] or event['input'] or event['output'] or event['title']
    return events


def merge_event(previous, incoming):
    """Merge records that share an exact call or block ID from one session."""
    merged = dict(previous)
    identity = ('title', 'category', 'lane', 'time', 'input', 'tool', 'file_action', 'agent_type', 'mcp_server')
    for key, value in incoming.items():
        if value in (None, '', []):
            continue
        if incoming.get('part') in ('result', 'notification') and previous.get('part') not in ('result', 'notification') and key in identity:
            continue
        if key == 'part' and previous.get('part') == 'call':
            continue
        if key == 'state' and value == 'running' and previous.get('state') not in (None, '', 'running', 'recorded'):
            continue
        if key == 'badges':
            merged[key] = list(dict.fromkeys(previous.get('badges', []) + value))
            continue
        merged[key] = value
    if merged.get('input') or merged.get('output'):
        merged['detail'] = merged.get('input') or merged.get('output')
    return merged
