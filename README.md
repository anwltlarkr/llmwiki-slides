# llmwiki-slides

`llmwiki-slides` is a local-first presentation indexer for Markdown LLM wikis and
Obsidian vaults. It discovers presentation files, extracts slide-level text and
metadata, applies a controlled vocabulary, builds a fast SQLite FTS5 index, and
exports one searchable Markdown source page per deck.

The package does not upload presentations or call an LLM. Original files are
read-only inputs.

## Why this package

Presentation archives are not ordinary text corpora. A useful index needs to
retain slide boundaries, titles, notes, file metadata, visual-density signals,
and the difference between projects, topics, event types, and output types.
`llmwiki-slides` stores these dimensions separately instead of flattening them
into a noisy tag list.

## Current capabilities

- Recursive `.pptx`, `.ppsx`, `.odp`, `.ppt`, `.pps`, `.key`, and `.keynote` discovery.
- Direct, dependency-free PPTX/ODP slide text extraction.
- Slide titles, body text, speaker notes, image/table/chart counts, and theme colors.
- Byte-exact SHA-256 source hashes, including recursive content hashing for Keynote packages.
- Separate versioned text, media, structure, semantic-content, and design fingerprints.
- Scan sessions with root-level error reporting, presence history, and conservative missing detection.
- Evidence-backed exact-duplicate, possible-move, format-variant, and design-variant relations.
- Source-bound conversion manifests that reject stale derivatives.
- Incremental reindexing based on fast size/mtime hints, with an explicit full verification command.
- Weighted, multi-axis classification from a user-controlled TOML vocabulary.
- Frequent terms calculated only from slide content, not generated summaries or paths.
- SQLite WAL mode with a single writer and concurrent search readers.
- FTS5 search over titles, text, notes, filenames, paths, keywords, and classifications.
- Atomic Markdown source pages compatible with Obsidian, qmd, and the LLMwiki schema.
- Multiprocess extraction and classification, parallel root scanning, and resumable jobs.
- Visual-review flags for sparse or image-heavy decks.

Legacy PowerPoint and Keynote files are cataloged as `needs-conversion`. App-specific
conversion is intentionally serialized rather than run concurrently inside extraction
workers. Existing PPTX derivatives can be imported without opening either application.

## Install

Requires Python 3.11 or newer with SQLite FTS5 enabled.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/llmwiki-slides --version
```

For source-tree use without installation:

```sh
./bin/llmwiki-slides --version
```

## Configure

```sh
cp config.example.toml config.local.toml
```

Set the vault, scan roots, exclusion fragments, and optional vocabulary path in
`config.local.toml`. Personal paths and research vocabularies should remain outside
the public repository.

The optional qmd `config_home` setting selects an XDG configuration directory. It is
useful when a vault needs collection-specific ignore rules without changing a user's
global qmd configuration.

```toml
[vault]
path = "/path/to/ObsidianVault"
database = "wiki/90-meta/llmwiki-slides/index.sqlite3"
markdown_output = "wiki/10-sources/presentations"

[scan]
roots = ["/path/to/presentation/folders"]
exclude_fragments = ["/ObsidianVault/", "/node_modules/", "/.git/"]
```

## Use

```sh
llmwiki-slides --config config.local.toml doctor
llmwiki-slides --config config.local.toml scan
llmwiki-slides --config config.local.toml migrate
llmwiki-slides --config config.local.toml update
llmwiki-slides --config config.local.toml search "temperature projections"
llmwiki-slides --config config.local.toml search "temperature projections" --all-slides
llmwiki-slides --config config.local.toml status
llmwiki-slides --config config.local.toml export-csv
llmwiki-slides --config config.local.toml convert
llmwiki-slides --config config.local.toml convert --run-apps --limit 10
llmwiki-slides --config config.local.toml integrity check
llmwiki-slides --config config.local.toml integrity backfill
llmwiki-slides --config config.local.toml integrity relations --needs-review
llmwiki-slides --config config.local.toml integrity review 42 confirmed
llmwiki-slides --config config.local.toml qmd --embed
llmwiki-slides --config config.local.toml qmd-search "quarterly project review"
```

`watch` keeps the index current in one terminal while any number of other processes
search it:

```sh
llmwiki-slides --config config.local.toml watch --interval 60
llmwiki-slides --config config.local.toml search "regional climate model"
```

The writer commits one completed deck at a time. Searches continue to use the prior
complete version until the replacement is ready, so they never observe a partial deck.
Read-only commands refuse an older schema and direct the operator to run `migrate`;
the migration first creates a SQLite backup and then applies its DDL transactionally.

`convert` first reuses configured PPTX derivative folders. `--run-apps` additionally
opens Keynote or PowerPoint for files without a derivative, one presentation at a time;
the exported PPTX files are then extracted and classified in the process pool.

Derivative reuse is intentionally strict. A sidecar manifest must match the current
source SHA-256 and derivative SHA-256. Pre-v0.2 derivatives stay searchable as
`legacy-unverified`; they are not silently trusted. `convert --adopt-legacy` is an
explicit user decision to bind the currently observed source and derivative hashes.

`integrity backfill` incrementally fills missing exact source hashes and can be stopped
and resumed. `--verify-files` deliberately re-reads every present source, including
files whose fast size/mtime signature did not change. Matching bytes never prove that
a file was moved: when an old path is missing and an identical new path exists, the
tool records only a `possible-move` relation for human review.

`export-csv` writes a machine-readable catalog containing source paths, timestamps,
authors, slide and image counts, visual-review flags, classification axes, frequent
keywords, summaries, and conversion details. Use `--output` to choose another path.

## Controlled vocabulary

Each rule has one organizational axis. Supported axes are open-ended; the recommended
ones are `project`, `topic`, `presentation_type`, `event`, `audience`, and
`output_relation`.

```toml
[[rules]]
axis = "topic"
name = "example-topic"
patterns = ["specific phrase", "\\bACRONYM\\b"]
fields = ["filename", "title", "slide_titles", "body", "notes"]
min_score = 4
```

Evidence is weighted by location: filename 5, path 4, deck title 4, slide title 3,
speaker notes 1.5, and slide body 1. This lets a strong filename cue classify a deck
without allowing one incidental body mention to contaminate categories.

## Concurrency model

- Thread pool: filesystem discovery across independent roots.
- Process pool: hashing, OOXML extraction, tokenization, and classification.
- Single parent writer: SQLite transaction and atomic Markdown replacement.
- Independent readers: live FTS5 queries while indexing continues.
- Serialized adapters: Keynote, PowerPoint, OCR, and qmd updates.

See [docs/architecture.md](docs/architecture.md) for the job and storage model.
See [docs/integrity-v0.2.md](docs/integrity-v0.2.md) for identity semantics and safety invariants.

## Test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests create a minimal PPTX fixture in a temporary directory; no personal files
are required.

## GitHub release checklist

1. Follow the data and identity checks in [PRIVACY.md](PRIVACY.md).
2. Review `LICENSE` and package metadata.
3. Keep local configuration, SQLite databases, presentation files, generated pages,
   screenshots, and personal vocabularies untracked.
4. Run `git add --dry-run .` and inspect every path before committing.
5. Run the test suite using synthetic fixtures only.
6. Create an empty GitHub repository named `llmwiki-slides`.
7. Add the remote, push the main branch, and enable the included Actions workflow.
8. Add only synthetic or explicitly public screenshots and search output.

The exact `llmwiki-slides` name did not appear in an initial GitHub/PyPI web search,
but repository and distribution availability should be checked again immediately
before publication.
