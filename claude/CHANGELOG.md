# Changelog

## Unreleased

- Recognise the desktop app's `<session>.desktop-released.json` marker beside a missing
  transcript (local format observed in desktop Claude Code 2.1.309). A `delete` reason
  shows "Deleted in Claude Code - index only" with the recorded time; status reports
  `log_state: deleted`. A session deleted before any page indexed it no longer waits
  for a transcript that will never be written. The marker is read with a 4 KiB limit
  through no-follow handles; malformed or other markers fall back to the generic
  missing-transcript message.

## 0.1.0 (2026-10-30)

First Claude Code adaptation of codex-trajectory.

- Claude transcript adapter for Claude Code 2.1.185: user prompts, assistant text with
  recorded stop reasons, tool calls and results joined by `tool_use_id`, MCP, file
  actions, native search, plan and task-list tools, skills, background-task
  notifications, and compaction boundaries.
- Subagents linked by recorded IDs only: dispatch result `agentId`, subagent
  `.meta.json` `toolUseId`, per-record `agentId`. Unlinked helper transcripts are not
  shown.
- Session identity from hook `session_id` + `transcript_path`; pending registration for
  transcripts that do not exist yet; exact-filename lookup for manual registration.
- Subcommand CLI: `register`, `url`, `resume`, `hook`, `stop`.
- Installer for `~/.claude/settings.json` with dry-run, backups, idempotent install,
  owned-entry uninstall, an optional `/trajectory` command and a marked rule block in
  `~/.claude/CLAUDE.md` (`--no-rule` to skip).
- Removed the Codex exec-wrapper fold from the page; Claude transcripts have no
  wrapper records.
- Partial trailing lines no longer keep the index in an endless "indexing" state.
- Storage identity changes are revalidated by record hashes; changed transcripts rebuild
  the disposable index instead of failing.
- Distinct reconnect messages for expired access, offline service and unreadable
  transcripts; separate `claude-trajectory://` recovery scheme.
- Removed: Codex rollout parsing and zstd archive handling.
