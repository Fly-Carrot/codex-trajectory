"""Allowlisted public Codex rollout events. Never render arbitrary event objects."""

from datetime import datetime, timezone
import json
import math
import re
import shlex


def public_text(value):
    if isinstance(value, list):
        return "\n".join(filter(None, (public_text(v) for v in value)))
    if isinstance(value, dict):
        if value.get("type") in ("Text", "text", "input_text", "output_text"):
            return str(value.get("text", ""))
        return ""
    return value if isinstance(value, str) else ""


def redact(text):
    text = re.sub(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", str(text))
    text = re.sub(r"\b(?:sk-|ghp_|gho_|github_pat_|nvapi-)[A-Za-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(r"(http://127\.0\.0\.1:\d+/[^\s#]*#)[A-Za-z0-9_-]+", r"\1[redacted]", text)
    text = re.sub(r'''(?i)((?:[\w-]*(?:password|secret|api[_-]?key)[\w-]*|(?:[\w-]+[_-])?token)["']?\s*[:=]\s*["']?)[^\s"',;}]+''', r"\1[redacted]", text)
    text = re.sub(r"/Users/[^/\s]+", "~", text)
    return text[:2400] + ("\n[preview truncated]" if len(text) > 2400 else "")


def preview(value):
    if value is None:
        return ""
    return redact(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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
                event["title"] = "Assistant" if phase == "commentary" else "Final response"
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
        event.update(title="Assistant" if item["phase"] == "commentary" else "Final response",
                     detail=redact(public_text(item.get("content", item.get("text", "")))))
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
    elif t in ("CollabAgentToolCall", "collabToolCall"):
        event.update(lane="tools", category="AGENT", title=redact(item.get("tool", "Agent operation")),
                     input=preview(item.get("prompt")),
                     output=preview({k: item[k] for k in ("receiver_thread_ids", "agents_states") if k in item}))
    elif t in ("ContextCompaction", "contextCompaction"):
        event.update(lane="activity", category="CONTEXT", title="Context compacted",
                     detail="Context compaction recorded. Internal context is not displayed.")
    elif t in ("Plan", "plan"):
        event.update(category="PLAN", title="Plan update", detail=redact(public_text(item.get("text", ""))))
    elif t in ("WebSearch", "webSearch"):
        event.update(lane="tools", category="SEARCH", title="Web search", input=preview(item.get("query")))
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
