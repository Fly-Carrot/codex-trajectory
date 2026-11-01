"""Allowlisted public Codex rollout events. Never render arbitrary event objects."""

from datetime import datetime, timezone
import json
import math
import re
import shlex


SECRET_NAME = r'(?:[\w-]{0,64}(?:password|secret|api[_-]?key)[\w-]{0,64}|(?:[\w-]{1,64}[_-])?token)'
SECRET_KEY = re.compile(SECRET_NAME, re.I)
SECRET_ASSIGNMENT = re.compile(r'\b' + SECRET_NAME + r'''(?:\\*["'])?\s*[:=]\s*''', re.I)


def public_text(value):
    if isinstance(value, list):
        return "\n".join(filter(None, (public_text(v) for v in value)))
    if isinstance(value, dict):
        if value.get("type") in ("Text", "text", "input_text", "output_text"):
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
    text = re.sub(r"\b(?:sk-|ghp_|gho_|github_pat_|nvapi-)[A-Za-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(r"(http://127\.0\.0\.1:\d+/[^\s#]*#)[A-Za-z0-9_-]+", r"\1[redacted]", text)
    parts, end = [], 0
    for match in SECRET_ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        parts.append(text[end:match.end()])
        end, delimiter = credential_end(text, match.end())
        parts.append(delimiter + '[redacted]' + delimiter)
    text = ''.join(parts) + text[end:]
    text = re.sub(r"/Users/[^/\s]+", "~", text)
    return text


def redact_structure(value, depth=0):
    # Called only on already selected public fields, never on a whole event.
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


def tool_badges(item, command=None):
    badges = []
    plugin = item.get('plugin_name')
    if isinstance(plugin, str) and re.fullmatch(r'[\w .@/-]{1,80}', plugin):
        badges.append('Plugin: ' + redact(plugin))
    if command and type(item.get('exit_code')) is int and item['exit_code'] == 0:
        try:
            args = shlex.split(command)
        except ValueError:
            args = []
        if len(args) == 2 and args[0] in ('cat', '/bin/cat') and not re.search(r'[;|&<>`$\n]', command):
            match = re.search(r'/skills/(?:\.system/)?([^/]+)/SKILL\.md$', args[1])
            if match:
                badges.append('Skill: ' + redact(match[1]) + ' (loaded)')
    return badges


def normalize(record, offset):
    p = record.get("payload")
    if not isinstance(p, dict):
        return None
    stamp = record.get("timestamp", "")
    event = {"id": "offset:" + str(offset), "time": stamp if isinstance(stamp, str) else "",
             "lane": "activity", "category": "ASSISTANT", "title": "", "detail": "",
             "call_id": "", "state": "recorded", "input": "", "output": "",
             "turn_id": str(p.get("turn_id", "")), "duration_ms": None, "origin": "response"}
    kind = p.get("type")
    if record.get("type") == "response_item":
        if kind == "message":
            role, phase = p.get("role"), p.get("phase", p.get("channel"))
            if role == "user":
                event.update(lane="input", category="USER", title="User")
            elif role == "assistant" and phase in ("commentary", "final", "final_answer"):
                event["title"] = "Agent progress" if phase == "commentary" else "Final response"
                event['reply_phase'] = 'Progress' if phase == 'commentary' else 'Final'
            else:
                return None
            event["id"] = "item:" + str(p["id"]) if p.get("id") else event["id"]
            event["detail"] = redact(public_text(p.get("content", [])))
        elif kind in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"):
            result = kind.endswith("_output")
            call = str(p.get("call_id") or "")
            event.update(lane="tools", category="TOOL", call_id=call,
                         id="call:" + call if call else event["id"],
                         title="Tool result" if result else redact(p.get("name", "tool")),
                         state="returned" if result else "running")
            field = "output" if result else "input"
            value = p.get("output", "") if result else p.get("arguments", p.get("input", ""))
            event[field] = redact(public_text(value))
            event["detail"] = event[field]
            if result:
                event['output_time'] = event['time']
            elif p.get('name') in ('exec', 'functions.exec'):
                event['is_wrapper'] = True
        else:
            return None
        return event
    if record.get("type") != "event_msg":
        return None
    if kind in ("task_started", "task_complete", "task_completed", "turn_aborted"):
        event.update(lane="boundary", category="TURN", state=kind,
                     title={"task_started": "Turn started", "task_complete": "Turn ended",
                            "task_completed": "Turn ended", "turn_aborted": "Turn interrupted"}[kind])
        if p.get("turn_id"):
            event["id"] = "turn:" + str(p["turn_id"]) + (":start" if kind == "task_started" else ":end")
        return event
    if kind not in ("item_started", "item_completed") or not isinstance(p.get("item"), dict):
        return None
    item = p["item"]
    t = item.get("type")
    # UserMessage has a different host ID from its response_item mirror. Use the
    # response_item as its sole source, rather than deduplicating by text/guessing.
    if t in ("Reasoning", "reasoning", "UserMessage", "userMessage"):
        return None
    event.update(origin="structured", state=str(item.get("status") or ("running" if kind == "item_started" else "completed")))
    if t in ("AgentMessage", "agentMessage"):
        if item.get("phase") not in ("commentary", "final_answer", "final"):
            return None
        event.update(title="Agent progress" if item["phase"] == "commentary" else "Final response",
                     detail=redact(public_text(item.get("content", item.get("text", "")))))
        event['reply_phase'] = 'Progress' if item['phase'] == 'commentary' else 'Final'
    elif t in ("CommandExecution", "commandExecution"):
        command = item.get('command', 'Command')
        display = command
        if isinstance(command, list) and all(isinstance(arg, str) for arg in command):
            display = command[2] if len(command) == 3 and command[1] in ('-c', '-lc') else shlex.join(command)
            command = shlex.join(command)
        event.update(lane="tools", category="COMMAND", title=redact(display),
                     input=preview(command), output=redact(public_text(item.get("aggregated_output") or item.get("formatted_output") or item.get("stdout", ""))))
        if item.get("stderr"):
            event["output"] = redact(event["output"] + "\n" + public_text(item["stderr"]))
        if isinstance(item.get("exit_code"), int):
            event["exit_code"] = item["exit_code"]
            if item["exit_code"] != 0:
                event["state"] = "failed"
        event['badges'] = tool_badges(item, display if isinstance(display, str) else None)
    elif t in ("FileChange", "fileChange"):
        changes = item.get("changes", {})
        if isinstance(changes, dict):
            lines = [str(path) + " (" + str(change.get("type", "change")) + ")"
                     for path, change in changes.items() if isinstance(change, dict)]
        elif isinstance(changes, list):
            lines = [str(c.get("path", "")) + " (" + str(c.get("kind", "change")) + ")" for c in changes if isinstance(c, dict)]
        else:
            lines = []
        event.update(lane="tools", category="FILE", title="File changes", output=redact("\n".join(lines)))
    elif t in ("McpToolCall", "mcpToolCall"):
        result = item.get("result")
        output = public_text(result.get("content", [])) if isinstance(result, dict) else public_text(result)
        event.update(lane="tools", category="MCP", title=redact(str(item.get("server", "MCP")) + "." + str(item.get("tool", "call"))),
                     input=preview(item.get("arguments")), output=redact(output))
        if isinstance(result, dict) and result.get("isError"):
            event["state"] = "failed"
        event['badges'] = tool_badges(item)
    elif t in ("CollabAgentToolCall", "collabToolCall"):
        event.update(lane="tools", category="AGENT", title=redact(item.get("tool", "Agent operation")),
                     input=preview(item.get("prompt")),
                     output=preview({k: item[k] for k in ("receiver_thread_ids", "agents_states") if k in item}))
    elif t in ("ContextCompaction", "contextCompaction"):
        event.update(lane="activity", category="CONTEXT", title="Context compacted",
                     detail="Context compaction recorded. Internal context is not displayed.")
    elif t in ("Plan", "plan"):
        event.update(category="PLAN", title="Plan update", detail=redact(public_text(item.get("text", ""))))
    elif t in ("WebSearch", "webSearch") or (t == "Extension" and item.get("kind") == "web.search"):
        # Accept host-native search only, not browser commands or arbitrary extensions.
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        results = item.get("results")
        public_results = [
            {key: result[key] for key in ("title", "url", "snippet") if isinstance(result.get(key), str)}
            for result in results[:10] if isinstance(result, dict) and result.get("type") == "text_result"
        ] if isinstance(results, list) else []
        event.update(lane="tools", category="SEARCH", title="Web search",
                     input=preview(item.get("query") or action.get("query") or action.get("queries")),
                     output=preview(public_results) if public_results else "")
    else:
        return None
    item_id = str(item.get("id") or "")
    if item_id:
        prefix = "call:" if event["lane"] == "tools" else "item:"
        event["id"] = prefix + item_id
        if event["lane"] == "tools":
            event["call_id"] = item_id
    start, end = p.get("started_at_ms"), p.get("completed_at_ms")
    if kind == 'item_completed' and event['lane'] == 'tools' and event['output']:
        event['output_time'] = (datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()
                                if number(end) and 0 <= end < 253402300799000 else event['time'])
    if number(start) and 0 <= start < 253402300799000:
        event["time"] = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
    # Timing belongs only to the actual operation, never to model inference.
    if event["lane"] == "tools":
        if number(start) and number(end) and end >= start:
            event["duration_ms"] = round(end - start, 3)
        else:
            duration = item.get("duration")
            if isinstance(duration, dict) and number(duration.get("secs")) and number(duration.get("nanos", 0)):
                ms = duration["secs"] * 1000 + duration.get("nanos", 0) / 1e6
                if ms >= 0:
                    event["duration_ms"] = round(ms, 3)
    event["detail"] = event["detail"] or event["input"] or event["output"] or event["title"]
    return event


def merge_event(previous, incoming):
    """Only callers with the same source identity may be merged."""
    merged = dict(previous)
    # Structured completions carry authoritative status; raw results are just
    # returned text and cannot overwrite a known failure.
    weaker = previous['origin'] == 'structured' and incoming['origin'] != 'structured'
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        if weaker and key not in ('input', 'output'):
            continue
        if weaker and previous.get(key):
            continue
        if incoming['title'] == 'Tool result' and key in ('title', 'category', 'time'):
            continue
        if key == 'state' and previous['state'] != 'running' and value == 'running':
            continue
        merged[key] = value
    if merged.get('input') or merged.get('output'):
        merged['detail'] = merged.get('input') or merged.get('output')
    return merged
