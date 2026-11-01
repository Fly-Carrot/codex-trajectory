# Event source map

Verified against Claude Code **2.1.185** (desktop entrypoint and `claude -p`) on
macOS, 2026-10-30. Only record structure was sampled: field names, types and
enumerations. No prompt text, tool output or credentials were copied.

Legend for **Stability**:

- **Documented**: described in the official hooks documentation.
- **Observed**: present in local transcripts of this version only. Claude Code does
  not publish a transcript schema, so treat it as a private format that may change.
- **Not verified**: plausible from documentation or other versions, but no local
  sample exists. Parsed only if the exact shape appears; never synthesized.

## Session identity and registration

| Need | Source | Stability | Notes |
| --- | --- | --- | --- |
| Exact session ID + transcript path | `SessionStart` / `UserPromptSubmit` hook stdin: `session_id`, `transcript_path`, `cwd`, `hook_event_name` | Documented, confirmed locally | `transcript_path` may not exist yet: on a new session it is created after both hooks have run. Registration accepts a pending path. |
| New vs resumed | `SessionStart.source` = `startup` / `resume` / `clear` / `compact` | Documented, confirmed `startup`, `resume` | Resume appends to the **same** file and keeps the ID. |
| Fork | `--fork-session` | Observed | `SessionStart` reports the **parent** ID with `source: resume`; the fork's new ID first appears at `UserPromptSubmit`. The fork file is a full copy with rewritten `sessionId`; no parent-link field exists. Both hooks are therefore needed. |
| Session ID inside Claude's shell | `CLAUDE_CODE_SESSION_ID` env var in the Bash tool | Observed | Used only as the default for `claude-trajectory url/resume`. Never used to pick a file. |
| Record ownership | per-record `sessionId` | Observed | Records carrying another `sessionId` are skipped and counted. |
| Transcript location | `<claude home>/projects/<encoded cwd>/<session_id>.jsonl` | Observed | Path must match this shape exactly. |
| Session deleted in the desktop app | `<session_id>.desktop-released.json` beside the removed transcript: `{v: 1, releasedAt, reason}`, `reason: delete` | Observed (desktop Claude Code 2.1.309, 2026-10-31) | Read only when no main transcript is readable, 4 KiB limit. Other reasons or shapes are not interpreted. The transcript itself is gone; only events indexed earlier remain. |

## Timeline events

| Track | Read from transcript | Stability | Hook needed? |
| --- | --- | --- | --- |
| User | `type:user`, `message.content` string or text blocks, `origin.kind` absent or `human`, not `isMeta`, not `isCompactSummary` | Observed | No |
| Turn boundary | `promptId` on user records (prompt and tool results) | Observed | No |
| Command | `tool_use` named `Bash`, `BashOutput`, `KillShell`, `PowerShell`; result `is_error` | Observed (`Bash`) | No |
| File | `tool_use` `Read` / `Write` / `Edit` / `MultiEdit` / `NotebookEdit`, action kept separately | Observed (`Read`) | No |
| MCP | `tool_use` named `mcp__<server>__<tool>` | Observed | No |
| Subagents | `Agent`/`Task` `tool_use` (dispatch), its `tool_result` + `toolUseResult.agentId`/`status`; `subagents/agent-<id>.jsonl` linked by `.meta.json` `toolUseId` and record `agentId`; `queued_command` attachments: `origin.kind: task-notification` with `<tool-use-id>`, and `origin.kind: peer` with `handback: true` and `from: <agentId>` | Observed | No |
| Context | `type:system`, `subtype:compact_boundary` | **Not verified** | No local sample; `PostCompact` hook exists but is not installed. |
| Plan | `EnterPlanMode`, `ExitPlanMode` (proposal), `TodoWrite`/`TaskCreate`/`TaskUpdate` (task list) | Tool names observed; records **not verified** | No |
| Search | `WebSearch`, `Grep`, `Glob` | Tool names observed; records **not verified** | No |
| Other Tools | Any other `tool_use` (`WebFetch`, `ToolSearch`, `Skill`, …) | Observed | No |
| Tool Results | `tool_result` blocks joined on `tool_use_id` | Observed | No |
| Artifacts | Markdown file links in a final reply, checked for existence under the record `cwd` | Observed | No |
| Agent | `type:assistant` text blocks. `stop_reason: end_turn` → Final, `tool_use` → Progress, otherwise no phase. `isApiErrorMessage` → error. | Observed | No |

Hooks `SubagentStart`, `SubagentStop`, `PreToolUse`, `PostToolUse` and `PreCompact` are
**not installed**: the transcript already carries exact IDs for these, so hooks would
only add duplicate events and per-call overhead.

## Deliberately not shown

| Data | Why |
| --- | --- |
| `thinking` / `redacted_thinking` blocks | Hidden reasoning. Not parsed or indexed. |
| `api-request`, `api-request-blob`, `api-request-shape` | Raw model request bodies. Record type is never parsed. |
| `attachment` (except the two `queued_command` origins above) | System context, tool listings, hook bookkeeping. |
| `isMeta` user records | Injected skill or system content, not user input. |
| `isCompactSummary` user records | Internal summary text. |
| `file-history-snapshot`, `last-prompt`, `custom-title`, `agent-name`, `mode`, `atis-latch`, `queue-operation` | Client bookkeeping. |
| Subagent transcripts without a recorded link to a dispatch in this session | Internal helper agents are not counted as user-dispatched subagents. |

## No reliable source in 2.1.185

- **Operation duration** for most tools. Only explicit `toolUseResult` fields
  (`durationMs`, `totalDurationMs`, `durationSeconds`) are shown. Timestamp gaps
  include permission prompts and are not presented as durations.
- **Bash exit code** as a structured field. Failure comes from `is_error`; a code is
  shown only when the error text begins with `Exit code N`.
- **Plan approval**: the `ExitPlanMode` result is shown as recorded. The viewer does
  not label it approved or rejected.
- **Skill completion**: `Skill` calls are labelled *invoked*; a `SKILL.md` read is
  labelled *file read*. Neither means the skill finished.
- **Plugin attribution**: only namespaced skills (`plugin:skill`) and MCP servers named
  `plugin_*` get a badge; nothing else is attributed.
