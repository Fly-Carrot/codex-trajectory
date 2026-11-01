# Secure reconnect Spec

## Goal

Let each panel recover without an agent repairing its chat. Keep one shared
observer, bounded caches, exact chat isolation and read-only source logs.

## Contract

1. **Automatic retry:** transient failures retry at bounded intervals without
   clearing displayed events. HTTP 401 stops credential use and offers native
   recovery. Existing per-instance access rotation is unchanged.
2. **No permanent web secret:** do not upgrade old bearer tokens into recovery
   keys. Security review rejected that design because a replacement page on an
   old loopback port could read the fragment. Trusted local recovery is smaller.
3. **UI:** one Reconnect button retries safely; failures back off to 30 seconds,
   hidden/paused pages do not poll. Keep selected chat, history and Follow state.
   No failure downgrades authentication or switches to a different chat.
4. **Offline/port change:** an explicit Open local recovery link invokes an
   optional macOS URL-handler app. It accepts only a registered thread identifier,
   asks native confirmation, calls the existing verified `resume`, then opens
   its verified local URL. No port scan, arbitrary URL, path or shell input.
   This may open a restored page in the default browser, not the same Codex tab.
5. **Browser limitation:** a fully closed service cannot serve HTML. An already
   loaded panel retains its recovery link; a cold browser error page requires
   launching the installed recovery app or the CLI. Do not promise a dead HTTP
   endpoint can restart itself. No extra always-on recovery daemon.
   Some embedded browsers block custom protocols. In that case use Finder to
   launch the recovery app; do not bypass the host's browser security policy.
6. **Security:** preserve Host/Origin checks, no CORS, no unauthenticated credential
   issuance, private state and verified service identity. No automatic protocol
   launch. Native recovery receives a chat ID only, never an origin or secret.
7. **Revocation:** restart continues to revoke old HTTP access links. The public
   native recovery URI has no credential and always requires local confirmation.

## Sequence

Spec and failing tests -> retry UI and native entry ->
regressions and live isolated restart/collision/multi-chat checks -> backed-up
local install. Preserve existing uncommitted history fixes.

## Acceptance

- Restart: old instance access fails; confirmed native recovery gets fresh access.
- Expired/missing/foreign tokens and forged service identity cannot read events.
- Many chats recover independently without switching identity.
- Port collision leaves the occupant untouched; native recovery returns new port.
- UI retries at bounded intervals, preserves reading state, and exposes manual
  local recovery without automatically launching external apps.
- Native URLs reject paths, queries, fragments, shell input and unknown chats.
- Original transcripts, global rules, hooks and unrelated configuration unchanged.

## Non-goals

No model calls, browser extension, launch daemon, raw log deletion, authentication
removal, automatic new tab, or GitHub publication in this change.

## Spec revision

The initial browser-key prototype was withdrawn before installation. This
document follows SPEC-20261020-002; SPEC-20261020-001 is superseded, not delivered.
