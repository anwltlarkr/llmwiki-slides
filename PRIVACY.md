# Privacy and publication checklist

`llmwiki-slides` is designed to run locally. The package does not upload source
presentations, extracted text, metadata, or search indexes, and it includes no
telemetry. Its application-conversion adapters operate on local files through
Keynote or PowerPoint.

The software is privacy-preserving only when generated data and local configuration
remain outside the public source repository. Extracted Markdown and SQLite indexes
can contain the full text, paths, authors, timestamps, notes, project names, and other
metadata from private presentations.

## Never publish by default

- Original or converted presentation files: `.ppt`, `.pptx`, `.pps`, `.ppsx`,
  `.key`, `.keynote`, and `.odp`.
- SQLite indexes, generated Markdown source pages, conversion caches, logs, and
  screenshots of real search results.
- Conversion sidecar manifests (`*.llmwiki-slides.json`), which contain local source
  and derivative paths plus cryptographic hashes.
- Local configuration files containing home-directory paths or scan roots.
- Personal controlled vocabularies, project names, institutional names, and private
  research terminology.
- API keys, access tokens, cookies, environment files, or application credentials.

These common artifacts are excluded by `.gitignore`, but ignore rules are not a
substitute for reviewing staged files.

## Identity considerations

Git commits store an author name and email address permanently in repository history.
Use a public identity intentionally, or configure a GitHub-provided no-reply email and
a suitable display name before the first commit. Publishing from a personal GitHub
account can itself disclose identity through the profile, activity, organization
membership, issues, discussions, releases, and linked email addresses.

For stronger separation, publish from a pseudonymous account or organization that has
no public link to private accounts, institutions, or reused usernames. Review account
profile fields and commit-signing identities as well as repository files.

## Before every public push

1. Run `git status --short --ignored` and `git add --dry-run .` from the repository
   root. Inspect every candidate path.
2. Search tracked and staged content for home paths, usernames, emails, institution
   names, project terms, credentials, and real presentation titles.
3. Confirm that tests and examples use only synthetic data.
4. Inspect images and binary assets manually; text scanners cannot reliably detect
   names or confidential figures inside them.
5. Review Git author and signing configuration before committing.
6. Check the complete commit history, not only the current working tree. Deleting a
   secret or private file in a later commit does not remove it from earlier history.
7. Review CI logs and release artifacts before making them public.

If private content is committed locally but has not been pushed, rewrite the local
history before publication. If a credential is ever pushed, revoke or rotate it even
after removing it from Git history.
