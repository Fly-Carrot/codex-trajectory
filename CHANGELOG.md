# Changelog

## 0.2.1 - 2026-10-06

### Bounded Cache And Reconnect Recovery

- Bound rebuildable main SQLite indexes to a shared 512 MiB budget. Evict
  least-recently-used inactive indexes, protect in-flight readers, and reserve
  concurrent growth. Original Codex logs are never deleted.
- Pause indexing with a visible capacity warning when active readers exhaust
  the budget; continue serving the last safe index and resume when room returns.
  Temporary SQLite journals and original logs are outside this index budget.
- Preserve pending history/restore anchors while indexing is paused, and retry
  disk-full failures after a bounded backoff. Treat invalid response encodings
  from unrelated port occupants as failed identity checks, not startup crashes.
- Keep the lightweight shared listener alive instead of stopping after one idle
  hour. Idle readers still expire, and hidden pages do not parse transcripts.
- Add `--resume --thread-id THREAD_ID` to restart/reconnect a registered chat
  using validated persisted source identity, without automatically opening tabs.
- Reuse the saved port when free; fall back safely if occupied, without stopping
  the occupant or sending it credentials. Reject malformed health responses.
- Preserve private URL fragments across client restoration, refresh credentials
  when a new link opens in the same tab, and show actionable recovery messages.
  Tokens still rotate on a real service restart; use a freshly verified link.
- Add regression coverage for capacity pressure, eviction, concurrent budgets,
  port conflicts, resume, and loss of browser session storage.

## 0.2.0 - 2026-10-03

### Reviewed History And Timeline

- Merge backfilled tool records in timestamp order, so older results cannot
  overwrite newer results. Rebuild the disposable pre-release index once.
- Keep surviving history readable when a registered segment disappears, including
  the latest segment. Missing sources are reported rather than substituted.
- Ignore generated SQLite indexes and runtime state in source control.
- Keep live updates flowing when browsing a full history window; preserve reading
  anchors and invalidate stale detail responses when an event changes.
- Restore old anchors across pages and initial indexing; show incomplete-history
  warnings rather than silently reporting a fully healthy view.
- Move cold index initialization outside the cross-chat lock. Return controlled
  errors for corrupt indexes, reject invalid headers and non-regular sources.
- Preserve custom rules across install/edit/reinstall/uninstall sequences.
- Redact quoted and JSON-escaped credentials in allowlisted public fields.

- Preserve verified same-chat log segments in a private, rebuildable SQLite index;
  refresh every registered chat connection after a broker restart.
- Isolate chat parsing locks; bound chunk work and rotate source scheduling fairly.
- Add recent-20-turn / 300-event pages, Load earlier, Latest, per-chat view recovery,
  lazy details and a 1,200-event sliding browser window with virtual ledger rows.
- Keep Codex originals read-only. Block premature pagination while indexing to
  avoid missing late-indexed records; migrate older index schemas safely.
- Bound credential-redaction work to avoid a regex stall on long source fields.

- Recognize native Extension/web.search records without reclassifying browser
  or shell activity. Expose bounded public search results, not internal fields.
- Show all twelve category rows permanently, without empty-state hints.
  Subagents and bottom Agent Progress / Final replies remain distinct.
- Keep timeline Follow independent of event inspection and ledger scrolling;
  resuming Follow no longer closes the selected event details.
- Separate Subagents operations from the bottom Agent message track, with
  explicit Progress / Final badges. Rename Output to Tool Results.
- Add Artifacts above Agent for final-reply report references verified to exist
  inside the exact session workspace. Do not imply task creation or read contents.
- Show evidence-backed Skill (loaded) / Plugin badges within tool rows.
- Fold known successful exec wrapper result projections with a reversible toggle;
  retain original events, errors and unknown results.
- Test workspace containment, symlinks, encoded traversal, removed references,
  missing workspace metadata, provenance and projection counts.

## 0.1.1 - 2026-10-03

### Fixed
- Removed reverse DNS from numeric loopback server binding. macOS CI captured
  the broker blocked in `socket.getfqdn` before writing its service record;
  increasing the startup timeout would only hide that dependency.
- Both shared and manual viewers now use the bound numeric address directly.
  Two regression tests reject any DNS lookup during server construction.
- Removed temporary CI stack diagnostics after identifying the cause. Existing
  timeout, authentication, containment and hook failure behavior are unchanged.

## 0.1.0 - 2026-10-03

Initial experimental public release.

### Added
- Compact per-category live timeline, searchable ledger and result inspector.
- Output track with recorded return timestamps; operation counts remain distinct.
- Shared local broker, exact-chat registration and lazy bounded readers.
- Optional reversible installer, UserPromptSubmit registration and final-link rule.
- English documentation, synthetic screenshot and CI for macOS/Linux.

### Hardened before release
- Do not forward prompt bodies to the bounded registration endpoint.
- Bound startup-lock waiting rather than immediately dropping competing hooks.
- Preserve authoritative structured results against weaker raw mirrors.
- Reject oversized persisted state before replacing valid state.
- Verify server identity before credential delivery; rotate chat tokens on restart.

### Known limits
- Local transcript schemas are version-sensitive; unsupported events are omitted.
- Final-link wording is guidance, not a native UI guarantee.
- Hook trust must be reviewed by the user. Windows is not supported yet.
