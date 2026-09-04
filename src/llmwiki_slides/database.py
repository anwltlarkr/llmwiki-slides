from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from itertools import combinations
from pathlib import Path
from typing import Any

from .models import Candidate


SCHEMA_VERSION = 2
RELATION_DETECTOR_VERSION = "1"
MAX_RELATION_GROUP = 100


class IndexDatabase:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self.migration_backup_path: Path | None = None
        if readonly:
            uri = f"file:{self.path}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "IndexDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _has_table(self, name: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _schema_version(self) -> int:
        if not self._has_table("metadata"):
            return 0
        row = self.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 1

    def _backup_for_migration(self) -> Path:
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.path.with_name(
            f"{self.path.stem}.pre-v{SCHEMA_VERSION}-{timestamp}{self.path.suffix or '.sqlite3'}"
        )
        counter = 1
        while destination.exists():
            destination = self.path.with_name(
                f"{self.path.stem}.pre-v{SCHEMA_VERSION}-{timestamp}-{counter}{self.path.suffix or '.sqlite3'}"
            )
            counter += 1
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        self.migration_backup_path = destination
        return destination

    def _ensure_column(self, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _finish_schema_migration(self, old_version: int, backup: str) -> None:
        for definition in (
            "presence_status TEXT NOT NULL DEFAULT 'unknown'",
            "last_seen_at TEXT",
            "last_seen_scan_id INTEGER",
            "missing_since TEXT",
            "source_sha256 TEXT NOT NULL DEFAULT ''",
            "source_hash_algorithm TEXT NOT NULL DEFAULT ''",
            "hash_quick_signature TEXT NOT NULL DEFAULT ''",
            "indexed_quick_signature TEXT NOT NULL DEFAULT ''",
        ):
            self._ensure_column("files", definition)
        for definition in (
            "derivative_path TEXT NOT NULL DEFAULT ''",
            "conversion_method TEXT NOT NULL DEFAULT ''",
            "source_sha256 TEXT NOT NULL DEFAULT ''",
            "source_hash_algorithm TEXT NOT NULL DEFAULT ''",
            "derivative_sha256 TEXT NOT NULL DEFAULT ''",
            "derivative_provenance TEXT NOT NULL DEFAULT ''",
            "fingerprint_version TEXT NOT NULL DEFAULT ''",
            "text_fingerprint TEXT NOT NULL DEFAULT ''",
            "media_fingerprint TEXT NOT NULL DEFAULT ''",
            "media_manifest_json TEXT NOT NULL DEFAULT '[]'",
            "structure_fingerprint TEXT NOT NULL DEFAULT ''",
            "semantic_fingerprint TEXT NOT NULL DEFAULT ''",
            "design_fingerprint TEXT NOT NULL DEFAULT ''",
        ):
            self._ensure_column("versions", definition)
        for definition in (
            "text_fingerprint TEXT NOT NULL DEFAULT ''",
            "structure_fingerprint TEXT NOT NULL DEFAULT ''",
            "design_fingerprint TEXT NOT NULL DEFAULT ''",
        ):
            self._ensure_column("slides", definition)
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_files_presence ON files(presence_status)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_files_source_sha ON files(source_sha256)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_semantic ON versions(semantic_fingerprint)"
        )
        if old_version < SCHEMA_VERSION:
            self.connection.execute(
                "UPDATE versions SET derivative_provenance='legacy-unverified' "
                "WHERE derivative_path<>'' AND derivative_provenance=''"
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, backup_path) VALUES(?, ?)",
                (SCHEMA_VERSION, backup),
            )
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed after migration: {integrity}")

    def validate_schema(self) -> None:
        version = self._schema_version()
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{version} is not readable by this command; run 'llmwiki-slides migrate'"
            )

    def initialize(self) -> None:
        if self.readonly:
            raise RuntimeError("cannot initialize a read-only database")
        existing = self._has_table("files")
        old_version = self._schema_version() if existing else 0
        if old_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{old_version} is newer than supported v{SCHEMA_VERSION}; refusing to write"
            )
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0] if existing else "ok"
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed before migration: {integrity}")
        backup = ""
        if existing and old_version < SCHEMA_VERSION:
            backup = str(self._backup_for_migration())

        try:
            self.connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                created_time TEXT,
                modified_time TEXT,
                quick_signature TEXT NOT NULL,
                is_package INTEGER NOT NULL DEFAULT 0,
                current_version_id INTEGER,
                status TEXT NOT NULL DEFAULT 'discovered',
                presence_status TEXT NOT NULL DEFAULT 'unknown',
                last_seen_at TEXT,
                last_seen_scan_id INTEGER,
                missing_since TEXT,
                source_sha256 TEXT NOT NULL DEFAULT '',
                source_hash_algorithm TEXT NOT NULL DEFAULT '',
                hash_quick_signature TEXT NOT NULL DEFAULT '',
                indexed_quick_signature TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                source_page TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (current_version_id) REFERENCES versions(id)
            );

            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source_sha256 TEXT NOT NULL DEFAULT '',
                source_hash_algorithm TEXT NOT NULL DEFAULT '',
                derivative_sha256 TEXT NOT NULL DEFAULT '',
                derivative_path TEXT NOT NULL DEFAULT '',
                derivative_provenance TEXT NOT NULL DEFAULT '',
                conversion_method TEXT NOT NULL DEFAULT '',
                fingerprint_version TEXT NOT NULL DEFAULT '',
                text_fingerprint TEXT NOT NULL DEFAULT '',
                media_fingerprint TEXT NOT NULL DEFAULT '',
                media_manifest_json TEXT NOT NULL DEFAULT '[]',
                structure_fingerprint TEXT NOT NULL DEFAULT '',
                semantic_fingerprint TEXT NOT NULL DEFAULT '',
                design_fingerprint TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                creator TEXT NOT NULL DEFAULT '',
                last_modified_by TEXT NOT NULL DEFAULT '',
                internal_created TEXT NOT NULL DEFAULT '',
                internal_modified TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                source_keywords TEXT NOT NULL DEFAULT '',
                spotlight_authors TEXT NOT NULL DEFAULT '',
                spotlight_creator TEXT NOT NULL DEFAULT '',
                spotlight_last_used TEXT NOT NULL DEFAULT '',
                theme_colors_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                classification_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                needs_visual_review INTEGER NOT NULL DEFAULT 0,
                visual_reasons_json TEXT NOT NULL DEFAULT '[]',
                slide_count INTEGER NOT NULL DEFAULT 0,
                text_char_count INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(file_id, content_hash),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS slides (
                id INTEGER PRIMARY KEY,
                version_id INTEGER NOT NULL,
                slide_number INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                word_count INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                table_count INTEGER NOT NULL DEFAULT 0,
                chart_count INTEGER NOT NULL DEFAULT 0,
                text_fingerprint TEXT NOT NULL DEFAULT '',
                structure_fingerprint TEXT NOT NULL DEFAULT '',
                design_fingerprint TEXT NOT NULL DEFAULT '',
                UNIQUE(version_id, slide_number),
                FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                quick_signature TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'index',
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(file_id, quick_signature, stage),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                status TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                roots_json TEXT NOT NULL DEFAULT '[]',
                errors_json TEXT NOT NULL DEFAULT '[]',
                discovered_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scan_roots (
                scan_id INTEGER NOT NULL,
                root_path TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (scan_id, root_path),
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS presence_events (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                scan_id INTEGER,
                event TEXT NOT NULL,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (scan_id) REFERENCES scan_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY,
                left_file_id INTEGER NOT NULL,
                right_file_id INTEGER NOT NULL,
                left_version_id INTEGER,
                right_version_id INTEGER,
                relation_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                detector_version TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(left_file_id, right_file_id, relation_type, detector_version),
                FOREIGN KEY (left_file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (right_file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (left_version_id) REFERENCES versions(id) ON DELETE SET NULL,
                FOREIGN KEY (right_version_id) REFERENCES versions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                backup_path TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_slides_version ON slides(version_id, slide_number);
            CREATE INDEX IF NOT EXISTS idx_relations_left ON relations(left_file_id, active);
            CREATE INDEX IF NOT EXISTS idx_relations_right ON relations(right_file_id, active);

            CREATE VIRTUAL TABLE IF NOT EXISTS slides_fts USING fts5(
                title,
                body,
                notes,
                filename,
                path,
                keywords,
                classification,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
            )
            self._finish_schema_migration(old_version, backup)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def record_scan(self, report: Any, *, config_fingerprint: str) -> int:
        roots = [root.to_dict() for root in report.roots]
        errors = [error for root in report.roots for error in root.errors]
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO scan_runs(status, config_fingerprint, roots_json, errors_json, discovered_count)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "discovered",
                config_fingerprint,
                json.dumps(roots, ensure_ascii=False, sort_keys=True),
                json.dumps(errors, ensure_ascii=False),
                len(report.candidates),
            ),
        )
        scan_id = int(cursor.lastrowid)
        cursor.executemany(
            """
            INSERT INTO scan_roots(scan_id, root_path, status, candidate_count, errors_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (scan_id, root.root, root.status, len(root.candidates), json.dumps(root.errors, ensure_ascii=False))
                for root in report.roots
            ],
        )
        self.connection.commit()
        return scan_id

    def finish_scan(self, scan_id: int, status: str) -> None:
        if status not in {"complete", "partial", "failed"}:
            raise ValueError(f"invalid final scan status: {status}")
        self.connection.execute(
            "UPDATE scan_runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, scan_id),
        )
        self.connection.commit()

    def register_candidates(
        self,
        candidates: Iterable[Candidate],
        *,
        force: bool = False,
        scan_id: int | None = None,
        queue_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        work: list[dict[str, Any]] = []
        cursor = self.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            for candidate in candidates:
                existing = cursor.execute(
                    """
                    SELECT id, quick_signature, indexed_quick_signature, status,
                           current_version_id, presence_status
                    FROM files WHERE path=?
                    """,
                    (candidate.path,),
                ).fetchone()
                needs_work = (
                    force
                    or existing is None
                    or existing["indexed_quick_signature"] != candidate.quick_signature
                )
                if existing is not None and existing["status"] in {"failed", "discovered", "queued", "indexing"}:
                    needs_work = True
                selected = needs_work and (queue_limit is None or len(work) < max(0, queue_limit))
                restored = existing is not None and existing["presence_status"] == "missing"
                observed_changed = existing is not None and existing["quick_signature"] != candidate.quick_signature

                cursor.execute(
                    """
                    INSERT INTO files(
                        path, filename, extension, size_bytes, mtime_ns, created_time,
                        modified_time, quick_signature, is_package, status,
                        presence_status, last_seen_at, last_seen_scan_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present', CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        filename=excluded.filename,
                        extension=excluded.extension,
                        size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns,
                        created_time=excluded.created_time,
                        modified_time=excluded.modified_time,
                        quick_signature=excluded.quick_signature,
                        is_package=excluded.is_package,
                        presence_status='present',
                        last_seen_at=CURRENT_TIMESTAMP,
                        last_seen_scan_id=excluded.last_seen_scan_id,
                        missing_since=NULL,
                        source_sha256=CASE WHEN ? THEN '' ELSE files.source_sha256 END,
                        source_hash_algorithm=CASE WHEN ? THEN '' ELSE files.source_hash_algorithm END,
                        hash_quick_signature=CASE WHEN ? THEN '' ELSE files.hash_quick_signature END,
                        status=CASE WHEN ? THEN 'queued' ELSE files.status END,
                        error=CASE WHEN ? THEN '' ELSE files.error END,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        candidate.path,
                        candidate.filename,
                        candidate.extension,
                        candidate.size_bytes,
                        candidate.mtime_ns,
                        candidate.created_time,
                        candidate.modified_time,
                        candidate.quick_signature,
                        int(candidate.is_package),
                        "queued" if selected else "discovered",
                        scan_id,
                        int(observed_changed),
                        int(observed_changed),
                        int(observed_changed),
                        int(selected),
                        int(selected),
                    ),
                )
                file_id = int(cursor.execute("SELECT id FROM files WHERE path=?", (candidate.path,)).fetchone()[0])
                if restored:
                    cursor.execute(
                        "INSERT INTO presence_events(file_id, scan_id, event) VALUES(?, ?, 'restored')",
                        (file_id, scan_id),
                    )
                if not selected:
                    continue
                cursor.execute(
                    """
                    UPDATE jobs SET status='superseded', finished_at=CURRENT_TIMESTAMP
                    WHERE file_id=? AND stage='index' AND status IN ('queued', 'running')
                      AND quick_signature<>?
                    """,
                    (file_id, candidate.quick_signature),
                )
                cursor.execute(
                    """
                    INSERT INTO jobs(file_id, quick_signature, stage, status)
                    VALUES(?, ?, 'index', 'queued')
                    ON CONFLICT(file_id, quick_signature, stage) DO UPDATE SET
                        status='queued', error='', queued_at=CURRENT_TIMESTAMP,
                        started_at=NULL, finished_at=NULL
                    """,
                    (file_id, candidate.quick_signature),
                )
                job_id = int(
                    cursor.execute(
                        "SELECT id FROM jobs WHERE file_id=? AND quick_signature=? AND stage='index'",
                        (file_id, candidate.quick_signature),
                    ).fetchone()[0]
                )
                item = candidate.to_dict()
                item["_file_id"] = file_id
                item["_job_id"] = job_id
                work.append(item)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return work

    def pending_index_count(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM files
                WHERE presence_status='present'
                  AND (indexed_quick_signature='' OR indexed_quick_signature<>quick_signature)
                """
            ).fetchone()[0]
        )

    @staticmethod
    def _under_root(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def reconcile_presence(
        self,
        *,
        scan_id: int,
        seen_paths: Iterable[str],
        complete_roots: Iterable[str | Path],
        extensions: Iterable[str],
        exclude_fragments: Iterable[str],
    ) -> dict[str, Any]:
        seen = set(seen_paths)
        roots = [Path(root).resolve(strict=False) for root in complete_roots]
        allowed = {value.casefold() for value in extensions}
        excluded = tuple(str(value) for value in exclude_fragments)
        changed: list[int] = []
        cursor = self.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            rows = cursor.execute("SELECT id, path, extension, presence_status FROM files").fetchall()
            for row in rows:
                path_text = str(row["path"])
                path = Path(path_text)
                if path_text in seen or row["extension"].casefold() not in allowed:
                    continue
                if any(fragment and fragment in path_text for fragment in excluded):
                    continue
                if not any(self._under_root(path, root) for root in roots):
                    continue
                if path.exists() or row["presence_status"] == "missing":
                    continue
                file_id = int(row["id"])
                cursor.execute(
                    """
                    UPDATE files SET presence_status='missing',
                                     missing_since=COALESCE(missing_since, CURRENT_TIMESTAMP),
                                     updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (file_id,),
                )
                cursor.execute(
                    "INSERT INTO presence_events(file_id, scan_id, event) VALUES(?, ?, 'missing')",
                    (file_id, scan_id),
                )
                changed.append(file_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"marked_missing": len(changed), "file_ids": changed}

    def presence_changed_file_ids(self, scan_id: int) -> list[int]:
        return [
            int(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT file_id FROM presence_events WHERE scan_id=?", (scan_id,)
            )
        ]

    def mark_running(self, job_ids: Iterable[int]) -> None:
        values = [(int(job_id),) for job_id in job_ids]
        self.connection.executemany(
            "UPDATE jobs SET status='running', attempts=attempts+1, started_at=CURRENT_TIMESTAMP WHERE id=?",
            values,
        )
        self.connection.executemany(
            """
            UPDATE files SET status='indexing', updated_at=CURRENT_TIMESTAMP
            WHERE id=(SELECT file_id FROM jobs WHERE id=? AND status='running')
            """,
            values,
        )
        self.connection.commit()

    def conversion_candidates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, path, filename, extension, size_bytes, mtime_ns, created_time,
                   modified_time, quick_signature, is_package, source_sha256,
                   source_hash_algorithm
            FROM files WHERE status='needs-conversion' AND presence_status='present'
            ORDER BY path
            """
        ).fetchall()
        return [
            {
                "_file_id": int(row["id"]),
                "path": row["path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "created_time": row["created_time"],
                "modified_time": row["modified_time"],
                "quick_signature": row["quick_signature"],
                "is_package": bool(row["is_package"]),
                "source_sha256": row["source_sha256"],
                "source_hash_algorithm": row["source_hash_algorithm"],
            }
            for row in rows
        ]

    def recover_interrupted_conversion_jobs(self) -> int:
        rows = self.connection.execute(
            "SELECT DISTINCT file_id FROM jobs WHERE stage='convert-index' AND status='running'"
        ).fetchall()
        file_ids = [int(row[0]) for row in rows]
        if not file_ids:
            return 0
        self.connection.execute(
            """
            UPDATE jobs SET status='queued', error='interrupted before completion',
                            started_at=NULL, finished_at=NULL
            WHERE stage='convert-index' AND status='running'
            """
        )
        self.connection.executemany(
            "UPDATE files SET status='needs-conversion', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [(file_id,) for file_id in file_ids],
        )
        self.connection.commit()
        return len(file_ids)

    def queue_conversion_import(
        self,
        candidate: dict[str, Any],
        derivative_path: Path,
        *,
        method: str,
        source_sha256: str,
        derivative_sha256: str,
        provenance: str,
    ) -> dict[str, Any]:
        signature = f"{candidate['quick_signature']}:derivative:{derivative_sha256}"
        file_id = int(candidate["_file_id"])
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO jobs(file_id, quick_signature, stage, status)
            VALUES(?, ?, 'convert-index', 'queued')
            ON CONFLICT(file_id, quick_signature, stage) DO UPDATE SET
                status='queued', error='', queued_at=CURRENT_TIMESTAMP,
                started_at=NULL, finished_at=NULL
            """,
            (file_id, signature),
        )
        row = cursor.execute(
            "SELECT id FROM jobs WHERE file_id=? AND quick_signature=? AND stage='convert-index'",
            (file_id, signature),
        ).fetchone()
        cursor.execute(
            """
            UPDATE jobs SET status='running', attempts=attempts+1, error='',
                            started_at=CURRENT_TIMESTAMP, finished_at=NULL
            WHERE id=?
            """,
            (row["id"],),
        )
        cursor.execute("UPDATE files SET status='indexing', updated_at=CURRENT_TIMESTAMP WHERE id=?", (file_id,))
        self.connection.commit()
        output = dict(candidate)
        output["_job_id"] = int(row["id"])
        output["_extract_path"] = str(derivative_path)
        output["_conversion_method"] = method
        output["_expected_source_sha256"] = source_sha256
        output["_expected_derivative_sha256"] = derivative_sha256
        output["_derivative_provenance"] = provenance
        return output

    def set_conversion_error(self, file_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE files SET status='needs-conversion', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error, file_id),
        )
        self.connection.commit()

    def store_result(self, result: dict[str, Any], *, extractor_version: str) -> tuple[int | None, int | None]:
        candidate = result["candidate"]
        file_id = int(candidate["_file_id"])
        job_id = int(candidate["_job_id"])
        status = result.get("extraction_status", "failed")
        if status == "failed":
            error = result.get("error", "unknown extraction failure")
            self.connection.execute(
                """
                UPDATE files SET status='failed', error=?, source_sha256=?,
                                 source_hash_algorithm=?, hash_quick_signature=quick_signature,
                                 updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    error,
                    result.get("source_sha256", ""),
                    result.get("source_hash_algorithm", ""),
                    file_id,
                ),
            )
            self.connection.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (error, job_id),
            )
            self.connection.commit()
            return None, None

        slides = result.get("slides", [])
        classification_json = json.dumps(result.get("classification", {}), ensure_ascii=False, sort_keys=True)
        keywords_json = json.dumps(result.get("keywords", []), ensure_ascii=False)
        theme_colors_json = json.dumps(result.get("theme_colors", []), ensure_ascii=False)
        visual_reasons_json = json.dumps(result.get("visual_review_reasons", []), ensure_ascii=False)
        media_manifest_json = json.dumps(result.get("media_manifest", []), ensure_ascii=False, sort_keys=True)
        text_char_count = sum(
            len(slide.get("title", "")) + len(slide.get("body", "")) + len(slide.get("notes", ""))
            for slide in slides
        )
        image_count = sum(int(slide.get("image_count", 0)) for slide in slides)

        cursor = self.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                """
                UPDATE files SET source_sha256=?, source_hash_algorithm=?,
                                 hash_quick_signature=quick_signature, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    result.get("source_sha256", ""),
                    result.get("source_hash_algorithm", ""),
                    file_id,
                ),
            )
            current = cursor.execute(
                """
                SELECT f.current_version_id, COALESCE(v.slide_count, 0) AS slide_count
                FROM files f LEFT JOIN versions v ON v.id=f.current_version_id WHERE f.id=?
                """,
                (file_id,),
            ).fetchone()
            if status == "needs-conversion" and current and int(current["slide_count"]) > 0:
                cursor.execute(
                    """
                    UPDATE files SET status='needs-conversion', error=?,
                                     indexed_quick_signature=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (result.get("error", ""), candidate["quick_signature"], file_id),
                )
                cursor.execute(
                    "UPDATE jobs SET status='succeeded', error='', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
                self.connection.commit()
                return file_id, int(current["current_version_id"])

            cursor.execute(
                """
                INSERT INTO versions(
                    file_id, content_hash, extractor_version,
                    source_sha256, source_hash_algorithm, derivative_sha256,
                    derivative_path, derivative_provenance, conversion_method,
                    fingerprint_version, text_fingerprint, media_fingerprint,
                    media_manifest_json, structure_fingerprint, semantic_fingerprint,
                    design_fingerprint, title, creator, last_modified_by,
                    internal_created, internal_modified, subject, source_keywords,
                    spotlight_authors, spotlight_creator, spotlight_last_used,
                    theme_colors_json, keywords_json, classification_json, summary,
                    needs_visual_review, visual_reasons_json, slide_count,
                    text_char_count, image_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, content_hash) DO UPDATE SET
                    extractor_version=excluded.extractor_version,
                    extracted_at=CURRENT_TIMESTAMP,
                    source_sha256=excluded.source_sha256,
                    source_hash_algorithm=excluded.source_hash_algorithm,
                    derivative_sha256=excluded.derivative_sha256,
                    derivative_path=excluded.derivative_path,
                    derivative_provenance=excluded.derivative_provenance,
                    conversion_method=excluded.conversion_method,
                    fingerprint_version=excluded.fingerprint_version,
                    text_fingerprint=excluded.text_fingerprint,
                    media_fingerprint=excluded.media_fingerprint,
                    media_manifest_json=excluded.media_manifest_json,
                    structure_fingerprint=excluded.structure_fingerprint,
                    semantic_fingerprint=excluded.semantic_fingerprint,
                    design_fingerprint=excluded.design_fingerprint,
                    title=excluded.title,
                    creator=excluded.creator,
                    last_modified_by=excluded.last_modified_by,
                    internal_created=excluded.internal_created,
                    internal_modified=excluded.internal_modified,
                    subject=excluded.subject,
                    source_keywords=excluded.source_keywords,
                    spotlight_authors=excluded.spotlight_authors,
                    spotlight_creator=excluded.spotlight_creator,
                    spotlight_last_used=excluded.spotlight_last_used,
                    theme_colors_json=excluded.theme_colors_json,
                    keywords_json=excluded.keywords_json,
                    classification_json=excluded.classification_json,
                    summary=excluded.summary,
                    needs_visual_review=excluded.needs_visual_review,
                    visual_reasons_json=excluded.visual_reasons_json,
                    slide_count=excluded.slide_count,
                    text_char_count=excluded.text_char_count,
                    image_count=excluded.image_count
                """,
                (
                    file_id, result["content_hash"], extractor_version,
                    result.get("source_sha256", ""), result.get("source_hash_algorithm", ""),
                    result.get("derivative_sha256", ""), result.get("derivative_path", ""),
                    result.get("derivative_provenance", ""), result.get("conversion_method", ""),
                    result.get("fingerprint_version", ""), result.get("text_fingerprint", ""),
                    result.get("media_fingerprint", ""), media_manifest_json,
                    result.get("structure_fingerprint", ""), result.get("semantic_fingerprint", ""),
                    result.get("design_fingerprint", ""), result.get("title", ""),
                    result.get("creator", ""), result.get("last_modified_by", ""),
                    result.get("internal_created", ""), result.get("internal_modified", ""),
                    result.get("subject", ""), result.get("source_keywords", ""),
                    result.get("spotlight_authors", ""), result.get("spotlight_creator", ""),
                    result.get("spotlight_last_used", ""), theme_colors_json, keywords_json,
                    classification_json, result.get("summary", ""),
                    int(bool(result.get("needs_visual_review"))), visual_reasons_json,
                    len(slides), text_char_count, image_count,
                ),
            )
            version_id = int(
                cursor.execute(
                    "SELECT id FROM versions WHERE file_id=? AND content_hash=?",
                    (file_id, result["content_hash"]),
                ).fetchone()[0]
            )

            old_slide_ids = [row[0] for row in cursor.execute("SELECT id FROM slides WHERE version_id=?", (version_id,))]
            if old_slide_ids:
                cursor.executemany("DELETE FROM slides_fts WHERE rowid=?", [(slide_id,) for slide_id in old_slide_ids])
            cursor.execute("DELETE FROM slides WHERE version_id=?", (version_id,))

            flat_keywords = " ".join(item.get("term", "") for item in result.get("keywords", []))
            flat_classification = " ".join(
                hit.get("name", "")
                for matches in result.get("classification", {}).values()
                for hit in matches
            )
            for slide in slides:
                cursor.execute(
                    """
                    INSERT INTO slides(
                        version_id, slide_number, title, body, notes, word_count,
                        image_count, table_count, chart_count, text_fingerprint,
                        structure_fingerprint, design_fingerprint
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id, slide["number"], slide.get("title", ""), slide.get("body", ""),
                        slide.get("notes", ""), int(slide.get("word_count", 0)),
                        int(slide.get("image_count", 0)), int(slide.get("table_count", 0)),
                        int(slide.get("chart_count", 0)), slide.get("text_fingerprint", ""),
                        slide.get("structure_fingerprint", ""), slide.get("design_fingerprint", ""),
                    ),
                )
                slide_id = cursor.lastrowid
                cursor.execute(
                    """
                    INSERT INTO slides_fts(
                        rowid, title, body, notes, filename, path, keywords, classification
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slide_id, slide.get("title", ""), slide.get("body", ""), slide.get("notes", ""),
                        candidate["filename"], candidate["path"], flat_keywords, flat_classification,
                    ),
                )

            cursor.execute(
                """
                UPDATE files SET current_version_id=?, status=?, error='',
                                 indexed_quick_signature=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (version_id, status, candidate["quick_signature"], file_id),
            )
            cursor.execute(
                "UPDATE jobs SET status='succeeded', error='', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
            self.connection.commit()
            return file_id, version_id
        except Exception:
            self.connection.rollback()
            raise

    def set_source_page(self, file_id: int, path: str | Path) -> None:
        self.connection.execute("UPDATE files SET source_page=? WHERE id=?", (str(path), file_id))
        self.connection.commit()

    def record_writer_failure(self, candidate: dict[str, Any], error: str) -> None:
        self.connection.execute(
            "UPDATE files SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error, int(candidate["_file_id"])),
        )
        self.connection.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (error, int(candidate["_job_id"])),
        )
        self.connection.commit()

    def hash_backfill_candidates(self, *, verify_files: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        where = "presence_status='present'"
        if not verify_files:
            where += " AND (source_sha256='' OR hash_quick_signature<>quick_signature)"
        query = f"SELECT id, path, quick_signature FROM files WHERE {where} ORDER BY path"
        parameters: list[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(0, limit))
        return [dict(row) for row in self.connection.execute(query, parameters)]

    def update_file_hash(self, file_id: int, value: str, algorithm: str, quick_signature: str) -> None:
        cursor = self.connection.cursor()
        row = cursor.execute("SELECT quick_signature FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None or row["quick_signature"] != quick_signature:
            raise RuntimeError("source changed after it was queued for hash backfill")
        cursor.execute(
            """
            UPDATE files SET source_sha256=?, source_hash_algorithm=?,
                             hash_quick_signature=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (value, algorithm, quick_signature, file_id),
        )
        self.connection.commit()

    def _changed_design_slides(self, left_version: int | None, right_version: int | None) -> list[int]:
        if not left_version or not right_version:
            return []
        left = {
            int(row["slide_number"]): row["design_fingerprint"]
            for row in self.connection.execute(
                "SELECT slide_number, design_fingerprint FROM slides WHERE version_id=?", (left_version,)
            )
        }
        right = {
            int(row["slide_number"]): row["design_fingerprint"]
            for row in self.connection.execute(
                "SELECT slide_number, design_fingerprint FROM slides WHERE version_id=?", (right_version,)
            )
        }
        return [number for number in sorted(set(left) | set(right)) if left.get(number) != right.get(number)]

    def _all_slide_numbers(self, left_version: int | None, right_version: int | None) -> list[int]:
        if not left_version or not right_version:
            return []
        return [
            int(row[0])
            for row in self.connection.execute(
                """
                SELECT slide_number FROM slides WHERE version_id=?
                UNION
                SELECT slide_number FROM slides WHERE version_id=?
                ORDER BY slide_number
                """,
                (left_version, right_version),
            )
        ]

    def rebuild_relations(self) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT f.id AS file_id, f.path, f.presence_status, f.source_sha256,
                   f.quick_signature, f.hash_quick_signature, f.indexed_quick_signature,
                   f.current_version_id AS version_id, v.semantic_fingerprint,
                   v.design_fingerprint, v.text_char_count, v.image_count,
                   v.source_sha256 AS indexed_source_sha256
            FROM files f LEFT JOIN versions v ON v.id=f.current_version_id
            """
        ).fetchall()
        source_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        semantic_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            hash_is_current = (
                row["source_sha256"]
                and row["hash_quick_signature"] == row["quick_signature"]
            )
            if hash_is_current:
                source_groups[row["source_sha256"]].append(row)
            indexed_content_is_current = (
                hash_is_current
                and row["indexed_quick_signature"] == row["quick_signature"]
                and row["indexed_source_sha256"] == row["source_sha256"]
            )
            if (
                indexed_content_is_current
                and row["semantic_fingerprint"]
                and (int(row["text_char_count"] or 0) >= 40 or int(row["image_count"] or 0) > 0)
            ):
                semantic_groups[row["semantic_fingerprint"]].append(row)

        cursor = self.connection.cursor()
        before = {
            (int(row["left_file_id"]), int(row["right_file_id"]), row["relation_type"]): (
                row["left_version_id"],
                row["right_version_id"],
                float(row["confidence"]),
                row["evidence_json"],
                row["review_status"],
            )
            for row in cursor.execute(
                """
                SELECT left_file_id, right_file_id, relation_type, left_version_id,
                       right_version_id, confidence, evidence_json, review_status
                FROM relations WHERE active=1 AND detector_version=?
                """,
                (RELATION_DETECTOR_VERSION,),
            )
        }
        cursor.execute("BEGIN IMMEDIATE")
        counts: dict[str, int] = defaultdict(int)
        skipped_groups = 0
        try:
            cursor.execute(
                "UPDATE relations SET active=0, updated_at=CURRENT_TIMESTAMP WHERE detector_version=?",
                (RELATION_DETECTOR_VERSION,),
            )

            def add(left: sqlite3.Row, right: sqlite3.Row, relation_type: str, confidence: float, evidence: dict[str, Any]) -> None:
                if int(left["file_id"]) > int(right["file_id"]):
                    left, right = right, left
                cursor.execute(
                    """
                    INSERT INTO relations(
                        left_file_id, right_file_id, left_version_id, right_version_id,
                        relation_type, confidence, evidence_json, detector_version, active
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(left_file_id, right_file_id, relation_type, detector_version)
                    DO UPDATE SET left_version_id=excluded.left_version_id,
                                  right_version_id=excluded.right_version_id,
                                  confidence=excluded.confidence,
                                  evidence_json=excluded.evidence_json,
                                  review_status=CASE
                                      WHEN relations.left_version_id IS excluded.left_version_id
                                       AND relations.right_version_id IS excluded.right_version_id
                                       AND relations.evidence_json=excluded.evidence_json
                                      THEN relations.review_status
                                      ELSE 'unreviewed'
                                  END,
                                  active=1,
                                  updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        int(left["file_id"]), int(right["file_id"]), left["version_id"], right["version_id"],
                        relation_type, confidence, json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                        RELATION_DETECTOR_VERSION,
                    ),
                )
                counts[relation_type] += 1

            for source_hash, group in source_groups.items():
                if len(group) < 2:
                    continue
                if len(group) > MAX_RELATION_GROUP:
                    skipped_groups += 1
                    continue
                for left, right in combinations(group, 2):
                    presence = {left["presence_status"], right["presence_status"]}
                    unambiguous_move = len(group) == 2 and presence == {"present", "missing"}
                    relation_type = "possible-move" if unambiguous_move else "exact-duplicate"
                    add(
                        left, right, relation_type, 1.0 if relation_type == "exact-duplicate" else 0.95,
                        {
                            "source_sha256": source_hash,
                            "left_presence": left["presence_status"],
                            "right_presence": right["presence_status"],
                            "identity_claim": "byte equality only",
                            "move_candidate_ambiguous": bool(
                                len(group) > 2 and presence == {"present", "missing"}
                            ),
                        },
                    )

            for semantic_hash, group in semantic_groups.items():
                if len(group) < 2:
                    continue
                if len(group) > MAX_RELATION_GROUP:
                    skipped_groups += 1
                    continue
                for left, right in combinations(group, 2):
                    if left["source_sha256"] and left["source_sha256"] == right["source_sha256"]:
                        continue
                    changed = self._changed_design_slides(left["version_id"], right["version_id"])
                    relation_type = "design-variant" if left["design_fingerprint"] != right["design_fingerprint"] else "format-variant"
                    if relation_type == "design-variant" and not changed:
                        changed = self._all_slide_numbers(left["version_id"], right["version_id"])
                    add(
                        left, right, relation_type, 0.99,
                        {
                            "semantic_fingerprint": semantic_hash,
                            "changed_design_slides": changed,
                            "content_changed_slides": [],
                        },
                    )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        after = {
            (int(row["left_file_id"]), int(row["right_file_id"]), row["relation_type"]): (
                row["left_version_id"],
                row["right_version_id"],
                float(row["confidence"]),
                row["evidence_json"],
                row["review_status"],
            )
            for row in self.connection.execute(
                """
                SELECT left_file_id, right_file_id, relation_type, left_version_id,
                       right_version_id, confidence, evidence_json, review_status
                FROM relations WHERE active=1 AND detector_version=?
                """,
                (RELATION_DETECTOR_VERSION,),
            )
        }
        changed_keys = {
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        }
        affected_file_ids = sorted({file_id for key in changed_keys for file_id in key[:2]})
        return {
            "active": dict(sorted(counts.items())),
            "skipped_large_groups": skipped_groups,
            "affected_file_ids": sorted(affected_file_ids),
        }

    def relations_for_file(self, file_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT r.id, r.relation_type, r.confidence, r.evidence_json, r.review_status,
                   CASE WHEN r.left_file_id=? THEN rf.path ELSE lf.path END AS related_path
            FROM relations r
            JOIN files lf ON lf.id=r.left_file_id
            JOIN files rf ON rf.id=r.right_file_id
            WHERE r.active=1 AND (r.left_file_id=? OR r.right_file_id=?)
            ORDER BY r.relation_type, related_path
            """,
            (file_id, file_id, file_id),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "relation_type": row["relation_type"],
                "confidence": float(row["confidence"]),
                "review_status": row["review_status"],
                "related_path": row["related_path"],
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    def relations_by_file(self) -> dict[int, list[dict[str, Any]]]:
        output: dict[int, list[dict[str, Any]]] = defaultdict(list)
        rows = self.connection.execute(
            """
            SELECT r.id, r.left_file_id, r.right_file_id, r.relation_type,
                   r.confidence, r.evidence_json, r.review_status,
                   lf.path AS left_path, rf.path AS right_path
            FROM relations r
            JOIN files lf ON lf.id=r.left_file_id
            JOIN files rf ON rf.id=r.right_file_id
            WHERE r.active=1
            ORDER BY r.relation_type, lf.path, rf.path
            """
        ).fetchall()
        for row in rows:
            common = {
                "id": int(row["id"]),
                "relation_type": row["relation_type"],
                "confidence": float(row["confidence"]),
                "review_status": row["review_status"],
                "evidence": json.loads(row["evidence_json"]),
            }
            output[int(row["left_file_id"])].append(
                {**common, "related_path": row["right_path"]}
            )
            output[int(row["right_file_id"])].append(
                {**common, "related_path": row["left_path"]}
            )
        return dict(output)

    def relation_rows(self, *, needs_review: bool = False) -> list[dict[str, Any]]:
        where = "r.active=1"
        if needs_review:
            where += " AND r.review_status='unreviewed'"
        rows = self.connection.execute(
            f"""
            SELECT r.id, r.relation_type, r.confidence, r.review_status,
                   lf.path AS left_path, rf.path AS right_path, r.evidence_json
            FROM relations r
            JOIN files lf ON lf.id=r.left_file_id
            JOIN files rf ON rf.id=r.right_file_id
            WHERE {where}
            ORDER BY r.relation_type, lf.path, rf.path
            """
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "relation_type": row["relation_type"],
                "confidence": float(row["confidence"]),
                "review_status": row["review_status"],
                "left_path": row["left_path"],
                "right_path": row["right_path"],
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    def set_relation_review(self, relation_id: int, review_status: str) -> dict[str, Any]:
        if review_status not in {"confirmed", "rejected", "unreviewed"}:
            raise ValueError("review status must be confirmed, rejected, or unreviewed")
        cursor = self.connection.execute(
            "UPDATE relations SET review_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",
            (review_status, relation_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"active relation not found: {relation_id}")
        row = self.connection.execute(
            """
            SELECT r.id, r.relation_type, r.confidence, r.review_status,
                   lf.path AS left_path, rf.path AS right_path, r.evidence_json
            FROM relations r
            JOIN files lf ON lf.id=r.left_file_id
            JOIN files rf ON rf.id=r.right_file_id
            WHERE r.id=?
            """,
            (relation_id,),
        ).fetchone()
        return {
            "id": int(row["id"]),
            "relation_type": row["relation_type"],
            "confidence": float(row["confidence"]),
            "review_status": row["review_status"],
            "left_path": row["left_path"],
            "right_path": row["right_path"],
            "evidence": json.loads(row["evidence_json"]),
        }

    def integrity_report(self) -> dict[str, Any]:
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        missing = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN presence_status='present' AND f.source_sha256='' THEN 1 ELSE 0 END) AS source_hash,
                SUM(CASE WHEN v.id IS NOT NULL AND v.fingerprint_version='' THEN 1 ELSE 0 END) AS fingerprints,
                SUM(CASE WHEN v.derivative_path<>'' AND v.derivative_provenance='legacy-unverified' THEN 1 ELSE 0 END) AS legacy,
                SUM(
                    CASE WHEN v.id IS NOT NULL AND (
                        f.indexed_quick_signature<>f.quick_signature
                        OR (f.source_sha256<>'' AND v.source_sha256<>'' AND f.source_sha256<>v.source_sha256)
                        OR f.status='failed'
                        OR (f.status='needs-conversion' AND v.slide_count>0)
                    ) THEN 1 ELSE 0 END
                ) AS stale
            FROM files f LEFT JOIN versions v ON v.id=f.current_version_id
            """
        ).fetchone()
        report = {
            "database": str(self.path),
            "schema_version": self._schema_version(),
            "sqlite_integrity": integrity,
            "presence_unknown": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM files WHERE presence_status='unknown'"
                ).fetchone()[0]
            ),
            "present_without_source_hash": int(missing["source_hash"] or 0),
            "current_versions_without_fingerprints": int(missing["fingerprints"] or 0),
            "legacy_unverified_derivatives": int(missing["legacy"] or 0),
            "stale_index_bindings": int(missing["stale"] or 0),
            "active_relations": len(self.relation_rows()),
        }
        errors = [] if integrity == "ok" else [f"sqlite integrity: {integrity}"]
        warnings = []
        for key in (
            "presence_unknown",
            "present_without_source_hash",
            "current_versions_without_fingerprints",
            "legacy_unverified_derivatives",
            "stale_index_bindings",
        ):
            if report[key]:
                warnings.append(f"{key}: {report[key]}")
        report.update(
            {
                "errors": errors,
                "warnings": warnings,
                "healthy": not errors,
                "fully_verified": not errors and not warnings,
            }
        )
        return report

    def status(self) -> dict[str, Any]:
        file_counts = {row["status"]: row["count"] for row in self.connection.execute("SELECT status, COUNT(*) count FROM files GROUP BY status")}
        presence_counts = {
            row["presence_status"]: row["count"]
            for row in self.connection.execute("SELECT presence_status, COUNT(*) count FROM files GROUP BY presence_status")
        }
        job_counts = {row["status"]: row["count"] for row in self.connection.execute("SELECT status, COUNT(*) count FROM jobs GROUP BY status")}
        relation_counts = {
            row["relation_type"]: row["count"]
            for row in self.connection.execute("SELECT relation_type, COUNT(*) count FROM relations WHERE active=1 GROUP BY relation_type")
        }
        totals = self.connection.execute(
            """
            SELECT COUNT(*) files,
                   COALESCE(SUM(v.slide_count), 0) slides,
                   COALESCE(SUM(v.text_char_count), 0) text_characters,
                   COALESCE(SUM(v.image_count), 0) images,
                   COALESCE(SUM(v.needs_visual_review), 0) visual_review
            FROM files f LEFT JOIN versions v ON v.id=f.current_version_id
            """
        ).fetchone()
        return {
            "database": str(self.path), "schema_version": self._schema_version(),
            "files": int(totals["files"]), "slides": int(totals["slides"]),
            "text_characters": int(totals["text_characters"]), "images": int(totals["images"]),
            "needs_visual_review": int(totals["visual_review"]), "file_status": file_counts,
            "presence_status": presence_counts, "job_status": job_counts, "relations": relation_counts,
        }

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = re.findall(r"[A-Za-z0-9_+.-]+|[가-힣]+", query)
        return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

    def search(self, query: str, *, limit: int = 20, all_slides: bool = False) -> list[dict[str, Any]]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        fetch_limit = limit if all_slides else max(limit * 12, 100)
        rows = self.connection.execute(
            """
            SELECT f.path, f.filename, f.extension, f.status, f.presence_status,
                   f.missing_since, f.source_page, s.slide_number, s.title,
                   snippet(slides_fts, 1, '[', ']', ' ... ', 24) AS snippet,
                   v.summary, v.classification_json, v.keywords_json,
                   v.needs_visual_review,
                   bm25(slides_fts, 3.5, 1.0, 1.3, 2.5, 1.2, 1.4, 1.6) AS score
            FROM slides_fts
            JOIN slides s ON s.id=slides_fts.rowid
            JOIN versions v ON v.id=s.version_id
            JOIN files f ON f.id=v.file_id
            WHERE slides_fts MATCH ? AND f.current_version_id=v.id
            ORDER BY score, f.filename, s.slide_number
            LIMIT ?
            """,
            (fts_query, fetch_limit),
        ).fetchall()
        output = []
        seen_paths: set[str] = set()
        for row in rows:
            if not all_slides and row["path"] in seen_paths:
                continue
            seen_paths.add(row["path"])
            output.append(
                {
                    "path": row["path"], "filename": row["filename"], "extension": row["extension"],
                    "status": row["status"], "presence_status": row["presence_status"],
                    "missing_since": row["missing_since"] or "", "source_page": row["source_page"],
                    "slide_number": row["slide_number"], "slide_title": row["title"],
                    "snippet": row["snippet"], "summary": row["summary"],
                    "classification": json.loads(row["classification_json"]),
                    "keywords": json.loads(row["keywords_json"]),
                    "needs_visual_review": bool(row["needs_visual_review"]),
                    "score": round(float(row["score"]), 4),
                }
            )
            if len(output) == limit:
                break

        remaining = limit - len(output)
        if remaining <= 0:
            return output
        terms = re.findall(r"[A-Za-z0-9_+.-]+|[가-힣]+", query.casefold())
        clauses = " AND ".join("LOWER(filename || ' ' || path) LIKE ?" for _ in terms)
        parameters: list[Any] = [f"%{term}%" for term in terms]
        parameters.append(remaining)
        pending = self.connection.execute(
            f"""
            SELECT path, filename, extension, status, presence_status, missing_since, source_page, error
            FROM files WHERE current_version_id IS NULL AND {clauses}
            ORDER BY modified_time DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
        output.extend(
            {
                "path": row["path"], "filename": row["filename"], "extension": row["extension"],
                "status": row["status"], "presence_status": row["presence_status"],
                "missing_since": row["missing_since"] or "", "source_page": row["source_page"],
                "slide_number": None, "slide_title": "",
                "snippet": row["error"] or "Discovered; slide-level indexing is not complete.",
                "summary": "", "classification": {}, "keywords": [],
                "needs_visual_review": False, "score": None,
            }
            for row in pending
        )
        return output

    def _presentation_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        version_id = int(row["resolved_version_id"])
        slides = [
            dict(slide)
            for slide in self.connection.execute(
                """
                SELECT slide_number AS number, title, body, notes, word_count,
                       image_count, table_count, chart_count, text_fingerprint,
                       structure_fingerprint, design_fingerprint
                FROM slides WHERE version_id=? ORDER BY slide_number
                """,
                (version_id,),
            )
        ]
        candidate = {
            "path": row["path"], "filename": row["filename"], "extension": row["extension"],
            "size_bytes": row["size_bytes"], "mtime_ns": row["mtime_ns"],
            "created_time": row["source_created_time"], "modified_time": row["source_modified_time"],
            "quick_signature": row["quick_signature"], "is_package": bool(row["is_package"]),
        }
        indexed_source = row["indexed_source_sha256"]
        current_source = row["current_source_sha256"]
        return {
            "candidate": candidate, "content_hash": row["content_hash"],
            "extraction_status": row["file_status"], "presence_status": row["presence_status"],
            "missing_since": row["missing_since"] or "", "last_seen_at": row["last_seen_at"] or "",
            "source_sha256": current_source or indexed_source, "indexed_source_sha256": indexed_source,
            "source_hash_algorithm": row["current_source_hash_algorithm"] or row["indexed_source_hash_algorithm"],
            "source_content_stale": bool(
                row["indexed_quick_signature"] != row["quick_signature"]
                or
                (current_source and indexed_source and current_source != indexed_source)
                or (row["file_status"] == "needs-conversion" and row["derivative_path"])
                or row["file_status"] == "failed"
            ),
            "derivative_sha256": row["derivative_sha256"], "derivative_path": row["derivative_path"],
            "derivative_provenance": row["derivative_provenance"], "conversion_method": row["conversion_method"],
            "fingerprint_version": row["fingerprint_version"], "text_fingerprint": row["text_fingerprint"],
            "media_fingerprint": row["media_fingerprint"], "media_manifest": json.loads(row["media_manifest_json"]),
            "structure_fingerprint": row["structure_fingerprint"],
            "semantic_fingerprint": row["semantic_fingerprint"], "design_fingerprint": row["design_fingerprint"],
            "title": row["title"], "creator": row["creator"], "last_modified_by": row["last_modified_by"],
            "internal_created": row["internal_created"], "internal_modified": row["internal_modified"],
            "subject": row["subject"], "source_keywords": row["source_keywords"],
            "spotlight_authors": row["spotlight_authors"], "spotlight_creator": row["spotlight_creator"],
            "spotlight_last_used": row["spotlight_last_used"],
            "theme_colors": json.loads(row["theme_colors_json"]),
            "keywords": json.loads(row["keywords_json"]),
            "classification": json.loads(row["classification_json"]), "summary": row["summary"],
            "needs_visual_review": bool(row["needs_visual_review"]),
            "visual_review_reasons": json.loads(row["visual_reasons_json"]),
            "slides": slides, "relations": self.relations_for_file(int(row["resolved_file_id"])),
            "error": row["error"],
        }

    def presentation_for_file(self, file_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT f.*, v.*, f.id AS resolved_file_id, v.id AS resolved_version_id,
                   f.status AS file_status, f.created_time AS source_created_time,
                   f.modified_time AS source_modified_time,
                   f.source_sha256 AS current_source_sha256,
                   f.source_hash_algorithm AS current_source_hash_algorithm,
                   v.source_sha256 AS indexed_source_sha256,
                   v.source_hash_algorithm AS indexed_source_hash_algorithm
            FROM files f JOIN versions v ON v.id=f.current_version_id WHERE f.id=?
            """,
            (file_id,),
        ).fetchone()
        return self._presentation_from_row(row) if row else None

    def current_presentations(self) -> Iterable[tuple[int, dict[str, Any]]]:
        rows = self.connection.execute(
            """
            SELECT f.*, v.*, f.id AS resolved_file_id, v.id AS resolved_version_id,
                   f.status AS file_status, f.created_time AS source_created_time,
                   f.modified_time AS source_modified_time,
                   f.source_sha256 AS current_source_sha256,
                   f.source_hash_algorithm AS current_source_hash_algorithm,
                   v.source_sha256 AS indexed_source_sha256,
                   v.source_hash_algorithm AS indexed_source_hash_algorithm
            FROM files f JOIN versions v ON v.id=f.current_version_id ORDER BY f.path
            """
        )
        for row in rows:
            yield int(row["resolved_file_id"]), self._presentation_from_row(row)
