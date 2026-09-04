from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Candidate


SCHEMA_VERSION = "1"


class IndexDatabase:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
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

    def initialize(self) -> None:
        self.connection.executescript(
            """
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
                derivative_path TEXT NOT NULL DEFAULT '',
                conversion_method TEXT NOT NULL DEFAULT '',
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

            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_slides_version ON slides(version_id, slide_number);

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
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        version_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(versions)")}
        if "derivative_path" not in version_columns:
            self.connection.execute("ALTER TABLE versions ADD COLUMN derivative_path TEXT NOT NULL DEFAULT ''")
        if "conversion_method" not in version_columns:
            self.connection.execute("ALTER TABLE versions ADD COLUMN conversion_method TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

    def register_candidates(self, candidates: Iterable[Candidate], *, force: bool = False) -> list[dict[str, Any]]:
        work: list[dict[str, Any]] = []
        cursor = self.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            for candidate in candidates:
                existing = cursor.execute(
                    "SELECT id, quick_signature, status, current_version_id FROM files WHERE path=?",
                    (candidate.path,),
                ).fetchone()
                needs_work = force or existing is None or existing["quick_signature"] != candidate.quick_signature
                if existing is not None and existing["status"] in {"failed", "discovered", "queued", "indexing"}:
                    needs_work = True

                cursor.execute(
                    """
                    INSERT INTO files(
                        path, filename, extension, size_bytes, mtime_ns, created_time,
                        modified_time, quick_signature, is_package, status
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        filename=excluded.filename,
                        extension=excluded.extension,
                        size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns,
                        created_time=excluded.created_time,
                        modified_time=excluded.modified_time,
                        quick_signature=excluded.quick_signature,
                        is_package=excluded.is_package,
                        discovered_at=CURRENT_TIMESTAMP,
                        status=CASE WHEN ? THEN 'queued' ELSE files.status END,
                        error=CASE WHEN ? THEN '' ELSE files.error END
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
                        "queued" if needs_work else "discovered",
                        int(needs_work),
                        int(needs_work),
                    ),
                )
                file_id = cursor.execute("SELECT id FROM files WHERE path=?", (candidate.path,)).fetchone()[0]
                if not needs_work:
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
                job_id = cursor.execute(
                    "SELECT id FROM jobs WHERE file_id=? AND quick_signature=? AND stage='index'",
                    (file_id, candidate.quick_signature),
                ).fetchone()[0]
                item = candidate.to_dict()
                item["_file_id"] = file_id
                item["_job_id"] = job_id
                work.append(item)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return work

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
                   modified_time, quick_signature, is_package
            FROM files WHERE status='needs-conversion'
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
            }
            for row in rows
        ]

    def queue_conversion_import(
        self,
        candidate: dict[str, Any],
        derivative_path: Path,
        *,
        method: str,
    ) -> dict[str, Any] | None:
        stat = derivative_path.stat()
        signature = f"{candidate['quick_signature']}:derivative:{stat.st_size}:{stat.st_mtime_ns}"
        file_id = int(candidate["_file_id"])
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO jobs(file_id, quick_signature, stage, status)
            VALUES(?, ?, 'convert-index', 'queued')
            ON CONFLICT(file_id, quick_signature, stage) DO NOTHING
            """,
            (file_id, signature),
        )
        row = cursor.execute(
            "SELECT id, status FROM jobs WHERE file_id=? AND quick_signature=? AND stage='convert-index'",
            (file_id, signature),
        ).fetchone()
        if row["status"] == "succeeded":
            self.connection.commit()
            return None
        cursor.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, error='', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
        cursor.execute("UPDATE files SET status='indexing', updated_at=CURRENT_TIMESTAMP WHERE id=?", (file_id,))
        self.connection.commit()
        output = dict(candidate)
        output["_job_id"] = int(row["id"])
        output["_extract_path"] = str(derivative_path)
        output["_conversion_method"] = method
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
                "UPDATE files SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (error, file_id),
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
                INSERT INTO versions(
                    file_id, content_hash, extractor_version, derivative_path, conversion_method,
                    title, creator, last_modified_by,
                    internal_created, internal_modified, subject, source_keywords,
                    spotlight_authors, spotlight_creator, spotlight_last_used,
                    theme_colors_json, keywords_json, classification_json, summary,
                    needs_visual_review, visual_reasons_json, slide_count,
                    text_char_count, image_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, content_hash) DO UPDATE SET
                    extractor_version=excluded.extractor_version,
                    extracted_at=CURRENT_TIMESTAMP,
                    derivative_path=excluded.derivative_path,
                    conversion_method=excluded.conversion_method,
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
                    file_id,
                    result["content_hash"],
                    extractor_version,
                    result.get("derivative_path", ""),
                    result.get("conversion_method", ""),
                    result.get("title", ""),
                    result.get("creator", ""),
                    result.get("last_modified_by", ""),
                    result.get("internal_created", ""),
                    result.get("internal_modified", ""),
                    result.get("subject", ""),
                    result.get("source_keywords", ""),
                    result.get("spotlight_authors", ""),
                    result.get("spotlight_creator", ""),
                    result.get("spotlight_last_used", ""),
                    theme_colors_json,
                    keywords_json,
                    classification_json,
                    result.get("summary", ""),
                    int(bool(result.get("needs_visual_review"))),
                    visual_reasons_json,
                    len(slides),
                    text_char_count,
                    image_count,
                ),
            )
            version_id = cursor.execute(
                "SELECT id FROM versions WHERE file_id=? AND content_hash=?",
                (file_id, result["content_hash"]),
            ).fetchone()[0]

            old_slide_ids = [
                row[0] for row in cursor.execute("SELECT id FROM slides WHERE version_id=?", (version_id,))
            ]
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
                        image_count, table_count, chart_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        slide["number"],
                        slide.get("title", ""),
                        slide.get("body", ""),
                        slide.get("notes", ""),
                        int(slide.get("word_count", 0)),
                        int(slide.get("image_count", 0)),
                        int(slide.get("table_count", 0)),
                        int(slide.get("chart_count", 0)),
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
                        slide_id,
                        slide.get("title", ""),
                        slide.get("body", ""),
                        slide.get("notes", ""),
                        candidate["filename"],
                        candidate["path"],
                        flat_keywords,
                        flat_classification,
                    ),
                )

            cursor.execute(
                """
                UPDATE files SET current_version_id=?, status=?, error='', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (version_id, status, file_id),
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
        self.connection.execute(
            "UPDATE files SET source_page=? WHERE id=?",
            (str(path), file_id),
        )
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

    def status(self) -> dict[str, Any]:
        file_counts = {
            row["status"]: row["count"]
            for row in self.connection.execute("SELECT status, COUNT(*) count FROM files GROUP BY status")
        }
        job_counts = {
            row["status"]: row["count"]
            for row in self.connection.execute("SELECT status, COUNT(*) count FROM jobs GROUP BY status")
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
            "database": str(self.path),
            "files": int(totals["files"]),
            "slides": int(totals["slides"]),
            "text_characters": int(totals["text_characters"]),
            "images": int(totals["images"]),
            "needs_visual_review": int(totals["visual_review"]),
            "file_status": file_counts,
            "job_status": job_counts,
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
            SELECT f.path, f.filename, f.extension, f.status, f.source_page,
                   s.slide_number, s.title,
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
            output.append({
                "path": row["path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "status": row["status"],
                "source_page": row["source_page"],
                "slide_number": row["slide_number"],
                "slide_title": row["title"],
                "snippet": row["snippet"],
                "summary": row["summary"],
                "classification": json.loads(row["classification_json"]),
                "keywords": json.loads(row["keywords_json"]),
                "needs_visual_review": bool(row["needs_visual_review"]),
                "score": round(float(row["score"]), 4),
            })
            if len(output) == limit:
                break

        remaining = limit - len(output)
        if remaining <= 0:
            return output
        terms = re.findall(r"[A-Za-z0-9_+.-]+|[가-힣]+", query.casefold())
        clauses = " AND ".join("LOWER(filename || ' ' || path) LIKE ?" for _ in terms)
        parameters = [f"%{term}%" for term in terms]
        parameters.append(remaining)
        pending = self.connection.execute(
            f"""
            SELECT path, filename, extension, status, source_page, error
            FROM files
            WHERE current_version_id IS NULL AND {clauses}
            ORDER BY modified_time DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        output.extend(
            {
                "path": row["path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "status": row["status"],
                "source_page": row["source_page"],
                "slide_number": None,
                "slide_title": "",
                "snippet": row["error"] or "Discovered; slide-level indexing is not complete.",
                "summary": "",
                "classification": {},
                "keywords": [],
                "needs_visual_review": False,
                "score": None,
            }
            for row in pending
        )
        return output

    def current_presentations(self) -> Iterable[tuple[int, dict[str, Any]]]:
        rows = self.connection.execute(
            """
            SELECT f.*, v.*,
                   f.id AS resolved_file_id, v.id AS resolved_version_id,
                   f.created_time AS source_created_time,
                   f.modified_time AS source_modified_time
            FROM files f JOIN versions v ON v.id=f.current_version_id
            ORDER BY f.path
            """
        )
        for row in rows:
            slides = [
                dict(slide)
                for slide in self.connection.execute(
                    """
                    SELECT slide_number AS number, title, body, notes, word_count,
                           image_count, table_count, chart_count
                    FROM slides WHERE version_id=? ORDER BY slide_number
                    """,
                    (row["resolved_version_id"],),
                )
            ]
            candidate = {
                "path": row["path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "size_bytes": row["size_bytes"],
                "mtime_ns": row["mtime_ns"],
                "created_time": row["source_created_time"],
                "modified_time": row["source_modified_time"],
                "quick_signature": row["quick_signature"],
                "is_package": bool(row["is_package"]),
            }
            yield int(row["resolved_file_id"]), {
                "candidate": candidate,
                "content_hash": row["content_hash"],
                "extraction_status": row["status"],
                "derivative_path": row["derivative_path"],
                "conversion_method": row["conversion_method"],
                "title": row["title"],
                "creator": row["creator"],
                "last_modified_by": row["last_modified_by"],
                "internal_created": row["internal_created"],
                "internal_modified": row["internal_modified"],
                "subject": row["subject"],
                "source_keywords": row["source_keywords"],
                "spotlight_authors": row["spotlight_authors"],
                "spotlight_creator": row["spotlight_creator"],
                "spotlight_last_used": row["spotlight_last_used"],
                "theme_colors": json.loads(row["theme_colors_json"]),
                "keywords": json.loads(row["keywords_json"]),
                "classification": json.loads(row["classification_json"]),
                "summary": row["summary"],
                "needs_visual_review": bool(row["needs_visual_review"]),
                "visual_review_reasons": json.loads(row["visual_reasons_json"]),
                "slides": slides,
                "error": row["error"],
            }
