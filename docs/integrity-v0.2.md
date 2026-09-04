# Integrity and Identity Plan for v0.2

## Goal

`llmwiki-slides` must distinguish binary identity, presentation-content identity,
design identity, file presence, and human-confirmed file identity. A single MD5 or
SHA-256 value cannot answer all of those questions.

## Reviewed Decisions

Two independent reviews of the initial plan reached the same safety conclusions:

- A matching source SHA-256 proves identical bytes, not that a copy was physically
  moved. The automatic label is `possible-move` when one path is missing.
- Indexing state and filesystem presence are independent axes.
- Missing reconciliation is allowed only when the entire configured multi-root scan
  completed without an access or traversal error.
- Legacy converted derivatives remain searchable but unverified until reconverted or
  explicitly adopted by the user.
- Probable-revision matching and rendered perceptual hashes are deferred until their
  candidate blocking and review workflow can prevent noisy relations.

## Implementation Order

1. Add idempotent schema migration, SQLite backup, scan sessions, presence history,
   and separate index/presence state.
2. Add byte-exact source hashing, including framed recursive content hashing for
   Keynote packages, plus a resumable integrity backfill.
3. Add versioned text, media, structure, semantic, and design fingerprints.
4. Add source-bound derivative manifests and reject stale or unverified reuse by
   default.
5. Detect only evidence-backed `exact-duplicate`, `possible-move`, `format-variant`,
   and `design-variant` relations; never merge or delete records automatically.
6. Expose integrity fields in Markdown, CSV, search, status, and CLI reports.
7. Verify migration idempotency, scan failure safety, package changes, style-only
   changes, stale derivatives, relation stability, and preservation of the existing
   index.

## Safety Invariants

- Source presentations are read-only.
- Generated source pages are not deleted when a source disappears.
- A partial or failed scan cannot mark files missing.
- `missing_since` preserves the first observation; restoration is recorded as an
  event.
- Hashes computed while a source changes are discarded and retried.
- A derivative is reusable only when its manifest matches both current source bytes
  and derivative bytes.
- Legacy derivatives are never silently promoted to verified provenance.
- Relations retain paths and versions as separate records and always include evidence.
