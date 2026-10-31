# Verification report

Date: 2026-10-30. Host: Apple M4 Max, macOS 26.6.2, Python 3.14.6 (tests also on
3.12), Claude Code 2.1.185 (desktop entrypoint and the `claude` CLI). Node 24 for UI
logic tests.

## Automated tests

`python3 -B -m unittest discover -s tests`: **79 tests, all passing** on Python 3.14.6
and 3.12. Fixtures are synthetic and labelled as such. Coverage includes user and
assistant messages, command success and failure, file actions, MCP, search vs
non-search tools, plan tools, skill badges, async subagents with notifications and
hand-backs, linked versus unlinked subagent transcripts, duplicate records, partial
lines and delayed results, foreign-session records, wrong session files, symlink
replacement, missing segments, deleted transcripts, rewritten transcripts, inode
changes, oversized records, 60-turn paging, verified artifacts, HTTP auth, Host and
Origin checks, session isolation, pending registration, token rotation on restart,
port collision, two sessions in one service, hook output and failure handling,
bounded error log, installer dry-run / idempotency / uninstall / foreign files, the
installed hook running from an isolated copy, UI lane order, result projections,
Follow during detail viewing, reconnect messages and the native recovery contract.

## Real-host checks (not synthetic)

| Check | Result |
| --- | --- |
| Parse this development session's real transcript (3.3 MB, desktop app) | 0 skipped records, 0 foreign records. User, Bash, Read, MCP, WebFetch/ToolSearch, async `Agent` dispatch → notification → `completed`, linked subagent transcript and hand-back all landed in the expected rows. |
| Real hook payloads (`claude -p` with probe hooks) | `SessionStart` and `UserPromptSubmit` carry `session_id`, `transcript_path`, `cwd`, `hook_event_name`; the transcript file did not exist yet at either hook for a new session. |
| Isolated install (`CLAUDE_CONFIG_DIR` = scratch dir) + real `claude -p` new / `--resume` / `--fork-session` | Both sessions registered automatically, 0 hook errors, `url` verified for each. Claude Code itself recorded `hook_additional_context` with the link for each prompt, so the host accepted the context. |
| First run on a fresh config directory | Found and fixed: `projects/` did not exist at the first `SessionStart`. Regression test added. |
| Service restart | Old token → 401; `resume` → new token on the same saved port. |
| Upgrade over an older index layout | Found and fixed: a format change cleared rows but kept the old table shape (503). Old layouts are now dropped and rebuilt. Regression test added. |
| Index completeness on the real session | 109 tool calls and 7 text blocks in the raw log; the index holds exactly those plus the prompt, its turn boundary, one hand-back and two subagent operations (121 events). |
| CLAUDE.md rule block on a copy of the real `~/.claude/CLAUDE.md` and `settings.json` | Install then uninstall left both files byte-identical to the originals. The rule's `url --session-id "$CLAUDE_CODE_SESSION_ID"` command returned this session's verified link. |
| Browser rendering (built-in browser pane) | Twelve rows in order, details open without disabling Follow, tokens redacted in visible text. |

The CLI on this machine is not logged in (the desktop app has its own sign-in), so the
`claude -p` runs above produced no model output. They exercised the real hook and
transcript-writing paths only.

## Measured performance

| Measure | Value | Method |
| --- | --- | --- |
| Hook, service already running | median 84–86 ms (n=30) | Installed hook command, wall clock, includes Python start (~20 ms) |
| Hook, service must start | median 219 ms (n=3) | Same, after `stop` |
| Synthetic transcript | 102.4 MB, 24,800 records, 12,000 tool calls | `perf_broker` scratch script |
| First page | 86 ms, 300 events | First authenticated request |
| Full index | 2.1 s total CPU over 49 bounded refreshes | Wall clock is longer in the UI: one chunk per 1.5 s poll |
| Service memory | 29 MiB before, 39 MiB after indexing | `ps` RSS |
| Unchanged refresh | median 4 ms, max 5 ms (n=20) | |
| Appended record visible | next refresh, 5 ms | |
| Index size | 30.0 MB for the 102.4 MB source | SQLite file |
| Idle CPU with no page open | 0 s over 20 s | `ps` cumulative CPU time |
| Real session (3.3 MB) | refresh median 3 ms, service RSS 35 MiB | |

## Not verified

- `SessionStart` from the desktop app (startup, resume, compact) and registration of a
  brand-new desktop session. `UserPromptSubmit` **was** observed in the desktop app
  after installing into the real `~/.claude` on 2026-10-30: the link arrived as hook
  context on the next prompt, with no hook errors.
- Whether the model actually appends the link to its final reply.
- The `/trajectory` command in the desktop app or CLI.
- `compact_boundary` (Context row): no local sample exists; parsed only if that exact
  record appears.
- Plan-mode, `TodoWrite`, `WebSearch`, `Grep`, `Glob` records: tool names are known, but
  no local transcript contains them yet.
- The macOS recovery app and `claude-trajectory://` handler in a real browser (not
  installed).
- Linux, Python 3.10, 3.11 and 3.13.
- Very long real sessions and multi-day histories.
