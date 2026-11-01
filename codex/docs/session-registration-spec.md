# Independent-chat registration

## Scope

Register user-visible local Codex chats, including agent-created peer chats and
worktree chats. Keep runtime subagents attached to their parent, not separate
panels. This is an observer integration, not a task gate or scheduler.

## Evidence and failure

The installed runtime accepts `agent_created_thread` after a verified regression
fix. The installer still configures only UserPromptSubmit. SessionStart is
supported by the locally generated Codex protocol and official hooks reference:
https://learn.chatgpt.com/docs/hooks
Its source includes startup, resume, clear, compact and fork. Transcript paths
may be null. An API/schema declaration is not proof of desktop end-to-end delivery.

## Contract

1. Install SessionStart and retain UserPromptSubmit using the same command.
   Preserve unrelated hooks, existing rule prose, permission settings and trust.
   Require native user review of the new hook; never write trust automatically.
2. Accept SessionStart at all registration layers, not only CLI parsing. Return
   link context with the actual originating hook event name. No link unless the
   exact chat connection has been authenticated.
3. Reuse existing registration, service and token for repeated invocations.
   Do not restart the observer merely to register a new chat or refresh a link.
4. Retry only temporarily unavailable exact transcript inputs, at most three
   attempts with short bounded delays. Never retry identity/path violations,
   choose a different conversation, or scan all transcripts. Null paths defer
   without success claims; the next supported hook can retry registration.
5. Fail open with a short reason category, no prompt/token/raw-path logging,
   no blocking response and no extra model call. Preserve log authenticity,
   containment, no-follow access and independent runtime-subagent rejection.
6. Installation and uninstall are idempotent across both event groups. Back up
   first, detect concurrent edits, preserve hooks.json content not owned by us.
7. Existing unregistered peer chats may be registered once from exact validated
   source paths. This repairs availability, not historical replies or proof that
   their original creation triggered hooks.

## Delivery versus display

A running hook can inject a verified link into model context. Rendering that
link in a final answer remains model-followed guidance, not native UI injection.
Do not block completion, rewrite messages or auto-open tabs to enforce display.

## Acceptance

- Reproduce SessionStart rejection and missing installer entry before fixing.
- Exercise startup/resume/fork/compact, delayed file/header, permanent absence,
  null paths, wrong identity, subagent, unknown hook and duplicate registration.
- Verify both event contexts and unchanged authentication/path protections.
- Test installer dry-run, reinstall/uninstall, mixed custom hooks and upgrades.
- Run the full Python/JS regression suite and diff checks.
- Verify installed handler and repair known exact-ID peer registrations locally.
- After hook trust, use an actual desktop-created worktree chat to verify native
  hook invocation, registration, injected context and final link separately.
  Mark any unperformed native/manual test explicitly unverified. Do not confuse
  direct handler invocation or synthetic fixtures with end-to-end validation.

## Non-goals

No new background watcher, full-disk scan, extra per-tool hooks, private app
patching, trust bypass, transcript edits, KnowledgeOS dependency or UI redesign.

## Validation record (2026-10-30)

- Before: SessionStart was rejected as an unsupported event; installer regression
  found no owned SessionStart entry. Both failures were reproduced before repair.
- After: 238 automated tests passed, including JS timeline tests, installed-hook
  subprocess tests, readiness retries, identity rejection and installer ownership.
  Dry-run and diff whitespace checks passed.
- Installed after backup. Global rules, existing UserPromptSubmit definition and
  trust configuration were byte-preserved by installation. The user subsequently
  reviewed and trusted SessionStart through the native client.
- Native desktop test: created an independent worktree chat with the app's
  create-thread operation. Its actual session metadata showed an agent-created
  peer chat and worktree cwd. It registered automatically, received verified link
  context and returned its exact authenticated link without executing any tools.
  This validates the automatic creation-to-display chain, not just manual handler
  invocation. Which of the two installed hook events supplied the context was not
  retained in the inspected rollout; do not attribute that observation exclusively
  to SessionStart.
- Repaired four known unregistered peer chats from exact validated source paths.
  All previously registered access tokens remained unchanged; no observer restart.
- Native resume/fork/compaction and simultaneous desktop creation were not each
  exercised in this smoke test; supported-source and repeated-registration behavior
  have automated coverage. Hosts with null transcripts or missing lifecycle
  delivery still defer safely and are not claimed universally supported.
