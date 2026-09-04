# Architecture

## Pipeline DAG

```text
scan -> compare signature -> queue -> hash/extract -> classify -> SQLite commit -> Markdown export
                                      |
                                      +-> needs visual review
                                      +-> needs Keynote/PPT conversion
```

The unit of atomicity is one presentation version. A worker never writes SQLite or
Markdown. It returns an immutable result to the coordinator, which performs the only
database write and then replaces the generated Markdown file atomically.

## Parallel work

Independent files are submitted to a `ProcessPoolExecutor`. Filesystem roots are
scanned using a small thread pool. The default extraction process count is:

```text
max(1, min(6, CPU count - 2))
```

`spawn` is the default multiprocessing context. It avoids inheriting unsafe GUI and
application state on macOS and makes worker requirements explicit.

## Storage

The database contains:

- `files`: current source path, stat signature, state, and active version.
- `versions`: hash-addressed extraction and classification results.
- `slides`: slide-numbered text, notes, and structural counts.
- `slides_fts`: FTS5 projection for low-latency search.
- `jobs`: queued, running, succeeded, and failed indexing attempts.

WAL mode allows multiple readers to search while one coordinator commits results.
The `files.current_version_id` pointer changes in the same transaction that stores the
new slides, so a reader sees either the old complete version or the new complete one.

## Backpressure

Extraction concurrency is bounded by `workers.extract`. Keynote/PowerPoint automation,
OCR, and qmd embedding must use separate serialized or low-concurrency queues because
their resource and application-state constraints differ from OOXML parsing.

## Privacy boundary

The public package contains code and generic vocabulary examples. Local configuration,
source paths, the SQLite database, generated Markdown pages, and domain vocabulary stay
inside the user's vault.
