# Security

Do not post private transcripts, real token URLs or unredacted screenshots in issues.
To report a vulnerability, use private vulnerability reporting when available. If it
is not, open a minimal issue asking for a private contact, without exploit payloads
or personal data.

This observer is local and read-only. It is not a security monitor, a tamper-proof
audit log, or a defense against a compromised user account. See the README for the
trust boundary, local HTTP limitations and best-effort redaction.

## Hooks

The two installed hooks only register a session with the local service. They never
return `block` or `deny`, never change permissions or settings, never call a model,
and exit cleanly on any internal error. Failures are recorded as a single line
(timestamp, event name, exception type) in a private log capped at 32 KiB.

## Local recovery

The optional macOS `claude-trajectory://reconnect/SESSION_ID` handler is a local,
user-confirmed action, not a web authentication endpoint. It accepts an exact session
identifier only, uses the installed CLI and verifies the resulting service and session
before opening the URL. It never accepts an origin, filesystem path, shell command or
credential, and it uses its own scheme and bundle identifier.

A service restart revokes old web tokens. There is no persistent recovery secret in
HTML or browser storage, no automatic external-app launch, no CORS, and no
unauthenticated endpoint that issues access links.
