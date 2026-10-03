# v0.2.0 Release Review

This review covered the complete standalone runtime and pending changes since
v0.1.1: normalization, bounded readers, persistent history, HTTP broker, launcher,
installer, browser state, tests, public documentation and the synthetic preview.
Two independent reviewers checked frontend state and HTTP/installation safety;
the integration reviewer checked history indexing and release boundaries.

## Reproduced Findings And Repairs

| Priority | Failure reproduced before repair | Root cause and repair |
| --- | --- | --- |
| P1 | Install, add a custom rule, reinstall, uninstall loses the custom rule | Whole-file restoration used the first snapshot; preserve user-owned text across reinstall |
| P1 | Follow remains enabled but new events disappear at the 1,200-event limit | History protection starved the live tail; pin reading/selection anchors while retaining new tail events |
| P2 | Quoted or JSON-escaped credential values survive redaction | Text matching missed structured escaping and spaces; redact selected structured fields before serialization and handle complete quoted values |
| P2 | Backfill changes a newer tool result into an older result | Records were merged in ingestion order; use source timestamps for summaries and detail reconstruction |
| P2 | Removing one segment blocks other surviving history | Missing backfill/latest sources aborted the whole snapshot; return surviving data with a visible warning, without cross-chat substitution |
| P2 | A cold locked SQLite index delays a different chat | Schema initialization held the registry lock; initialize under the per-chat lock |
| P2 | Slow detail responses restore obsolete running state | Requests were tied only to event ID; bind details to the event revision and refresh changed selections |
| P2 | Old live-page anchors disappear after reopen | Saved null page cursors were not replayable; restore by stable canonical event ID |
| P2 | Cross-page selections permanently lose their inspector | Not-yet-loaded meant closed; retain pending detail restoration intent |
| P2 | Initial indexing fills the window and displaces the requested restore page | Temporary live fallback was treated as permanent history; replace it when the target is indexed |
| P2 | Missing-history warnings are ignored | Successful HTTP responses bypassed warning presentation; persist visible bounded warnings across page operations |
| P2 | Replacing a source with a FIFO hangs its reader | Blocking open did not enforce regular files; use nonblocking open and descriptor type validation |
| P2 | Invalid source header or corrupt index aborts a request unexpectedly | Validate header shapes and convert SQLite failures to controlled unavailable responses |

Regression tests reproduce each changed boundary. Existing authentication,
source-containment, no-reasoning, hook-failure and original-file preservation
tests remain part of the acceptance suite. See the repository's Tests workflow
for the exact candidate revision and macOS/Linux Python matrix results.

Local acceptance: 140 standalone tests, including 42 frontend state tests;
installer dry-run; Python compilation; whitespace and public-data scans. A real
browser smoke using fictional events verified twelve lanes, virtual rows,
Load earlier, stable-anchor reload and Follow remaining enabled.

## Release Hygiene

- Public files exclude real transcripts, private runtime URLs, local usernames,
  machine paths, SQLite caches and credentials.
- The screenshot contains fictional demonstration data and was inspected.
- Original Codex logs are read-only; no retention deletion or permission widening.
- Runtime installation preserves unrelated rules/hooks. Installation does not
  kill existing observer processes; new code loads on the next observer start.

## Residual Limits

This is an experimental observer, not a complete audit archive or a security
boundary against a malicious process running under the same OS account.
Credential masking remains best-effort, not a promise that arbitrary transcript
prose is safe to publish. Supported event schemas are allowlisted; oversized or
unknown records may be omitted. First-time history indexing can take minutes.
The rebuildable disk cache currently has no automatic size-quota cleanup.
Virtualized history is deliberately bounded; retained anchors and live tail may
have gaps between them. Original source logs remain the complete local record.
