# Changelog

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
