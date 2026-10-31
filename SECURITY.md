# Security

Do not post private transcripts, real token URLs or unredacted screenshots in
issues. To report a vulnerability, use GitHub private vulnerability reporting
when available. If unavailable, open a minimal issue requesting a private contact
without exploit payloads or personal data.

This observer is local and read-only, not a security monitor, tamper-proof audit
log, or a defense against a compromised user account. See README for the trust
boundary, local HTTP limitations and best-effort redaction.

## Local recovery

The optional macOS `codex-trajectory://reconnect/THREAD_ID` handler is a local,
user-confirmed action, not a web authentication endpoint. It accepts an exact
chat identifier only, uses the installed launcher and verifies the resulting
service and chat before opening the URL. It never accepts an arbitrary origin,
filesystem path, shell command or bearer credential.

Service restart still revokes old web tokens. There is no persistent recovery
secret in HTML or browser storage, no automatic external-app launch, no CORS,
and no unauthenticated endpoint that issues access links. A website can offer
the protocol URI, but the native confirmation is required. Cancel it if you did
not request recovery. Python recovery and newly launched brokers use isolated
interpreters with explicit installed-module paths.
