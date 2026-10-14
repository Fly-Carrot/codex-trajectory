# Segment recovery

## Charter

Goal: keep exact-chat history readable when Codex compresses or archives an
already registered rollout, without modifying original transcripts or adding an
unbounded decompression cache. This is a focused failure diagnosis, not an
exhaustive comparison of observability products.

## Canonical sources

1. [Codex rollout compression](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/compression.rs):
   originating implementation of transparent plain/compressed rollout lookup.
2. [lnav](https://github.com/tstack/lnav) and its
   [performance documentation](https://docs.lnav.org/en/latest/performance.html):
   maintainer descriptions of decompression, renamed-file tracking and indexing.
3. [Perfetto](https://github.com/google/perfetto): a mature tracing/analysis system
   useful for studying timeline interaction, not a replacement transcript reader.
4. Local synthetic regression fixtures: direct evidence for this implementation.

Sources inspected on 2026-10-14. Upstream main branches may change. No third-party
code was copied and none of these projects is a runtime dependency.

## Findings ledger

| ID | Finding | Evidence |
| --- | --- | --- |
| F1 | A plain rollout path can have a compressed representation. | Codex `compression.rs`, plain/compressed lookup and reader functions. |
| F2 | Decompression, renamed-file following and time merging are established log-viewer concerns. | lnav README and performance documentation. |
| F3 | Timeline interaction and transcript storage are separate concerns. | Perfetto tracing/analysis scope; design inference, not a Codex compatibility claim. |
| F4 | The old reader fails after plain files disappear despite equivalent compressed/archive sources. | Failing-first `test_registered_segments_survive_archive_and_compression` and `test_cold_compressed_registration`. |

## Scope filter

Do not adopt a tracing backend or cloud telemetry stack for a local transcript
viewer. General observability performance numbers do not establish our latency.

## Object filter

lnav and Perfetto are references, not drop-in Codex adapters. The implemented
resolver must enforce exact thread identity, source boundaries and read-only
access independently. A matching filename alone is insufficient.

## Conclusions

- F1/F4: resolve one logical segment across plain/compressed representations;
  prefer plain when both exist. Also check its exact basename in the sibling
  `archived_sessions` directory. Do not scan arbitrary directories or other chats.
- F2/F4: separate incremental indexing from rendering and detail retrieval. Keep
  the existing small page window and 512 MiB shared rebuildable-index budget.
- F3: use Perfetto for future timeline design/debugging ideas only; no new service
  or import format is introduced in this repair.

## Next steps / Risks

- The decoder uses Python 3.14 `compression.zstd`, or `backports.zstd` on older
  supported Python. Missing decoder support is an explicit error, not an empty
  history or automatic package installation.
- Compressed indexing is forward-only, approximately 2 MiB per refresh plus one
  bounded record. One decoder is retained per active chat, with an 8 MiB maximum
  Zstandard window. Header validation is capped at 8 MiB. Idle reader eviction
  releases decoder resources. No decoded transcript copy is written to disk.
- Restart may need bounded forward replay to reach an existing logical offset.
  The interface remains in indexing state until coverage is ready; a missing
  segment still produces a warning and is not silently forgotten.
- Explicit detail lookup scans at most 16 MiB per referenced compressed record.
  More distant details return a clear limit notice; indexed summaries stay
  available. Automatic artifact discovery does not repeatedly decompress archived
  final replies during refresh; explicit detail inspection can resolve them.
- Source identity and per-record hashes guard detail reads. This is not a signed
  audit store or proof that a locally writable transcript cannot be altered.
- Updating installed files does not hot-patch an existing observer process.
  Restart the verified observer and obtain a fresh private link after updating;
  never terminate an unknown process merely because it occupies the saved port.
