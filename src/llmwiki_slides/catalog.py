from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .database import IndexDatabase


CATALOG_FIELDS = [
    "source_path",
    "source_page",
    "filename",
    "file_type",
    "status",
    "presence_status",
    "last_seen_at",
    "missing_since",
    "size_bytes",
    "created_time",
    "modified_time",
    "recently_opened",
    "title",
    "creator",
    "last_modified_by",
    "slide_count",
    "text_char_count",
    "image_count",
    "needs_visual_review",
    "visual_review_reasons",
    "theme_colors",
    "projects",
    "topics",
    "presentation_types",
    "events",
    "audiences",
    "output_relations",
    "frequent_keywords",
    "summary",
    "source_sha256",
    "source_hash_algorithm",
    "indexed_source_sha256",
    "fingerprint_version",
    "text_fingerprint",
    "media_fingerprint",
    "structure_fingerprint",
    "semantic_fingerprint",
    "design_fingerprint",
    "derivative_path",
    "derivative_sha256",
    "derivative_provenance",
    "conversion_method",
    "relations",
    "error",
]


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _axis(classification: dict[str, Any], name: str) -> str:
    return "; ".join(str(item.get("name", "")) for item in classification.get(name, []) if item.get("name"))


def _single_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def catalog_rows(database: IndexDatabase) -> list[dict[str, Any]]:
    relations_by_file = database.relations_by_file()
    rows = database.connection.execute(
        """
        SELECT f.id AS file_id, f.path, f.source_page, f.filename, f.extension, f.status,
               f.presence_status, f.last_seen_at, f.missing_since,
               f.size_bytes, f.created_time, f.modified_time, f.error,
               f.source_sha256, f.source_hash_algorithm,
               v.spotlight_last_used, v.title, v.creator, v.last_modified_by,
               v.slide_count, v.text_char_count, v.image_count,
               v.needs_visual_review, v.visual_reasons_json, v.theme_colors_json,
               v.classification_json, v.keywords_json, v.summary,
               v.source_sha256 AS indexed_source_sha256,
               v.fingerprint_version, v.text_fingerprint, v.media_fingerprint,
               v.structure_fingerprint, v.semantic_fingerprint, v.design_fingerprint,
               v.derivative_path, v.derivative_sha256, v.derivative_provenance,
               v.conversion_method
        FROM files f
        LEFT JOIN versions v ON v.id=f.current_version_id
        ORDER BY f.path
        """
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        classification = _json(row["classification_json"], {})
        keywords = _json(row["keywords_json"], [])
        relations = relations_by_file.get(int(row["file_id"]), [])
        output.append(
            {
                "source_path": row["path"],
                "source_page": row["source_page"],
                "filename": row["filename"],
                "file_type": row["extension"].lstrip("."),
                "status": row["status"],
                "presence_status": row["presence_status"],
                "last_seen_at": row["last_seen_at"] or "",
                "missing_since": row["missing_since"] or "",
                "size_bytes": row["size_bytes"],
                "created_time": row["created_time"] or "",
                "modified_time": row["modified_time"] or "",
                "recently_opened": row["spotlight_last_used"] or "",
                "title": _single_line(row["title"]),
                "creator": _single_line(row["creator"]),
                "last_modified_by": _single_line(row["last_modified_by"]),
                "slide_count": row["slide_count"] or 0,
                "text_char_count": row["text_char_count"] or 0,
                "image_count": row["image_count"] or 0,
                "needs_visual_review": bool(row["needs_visual_review"]),
                "visual_review_reasons": "; ".join(_json(row["visual_reasons_json"], [])),
                "theme_colors": "; ".join(_json(row["theme_colors_json"], [])),
                "projects": _axis(classification, "project"),
                "topics": _axis(classification, "topic"),
                "presentation_types": _axis(classification, "presentation_type"),
                "events": _axis(classification, "event"),
                "audiences": _axis(classification, "audience"),
                "output_relations": _axis(classification, "output_relation"),
                "frequent_keywords": "; ".join(
                    f"{item.get('term', '')}:{item.get('count', 0)}:{item.get('slide_count', 0)}"
                    for item in keywords
                    if item.get("term")
                ),
                "summary": _single_line(row["summary"]),
                "source_sha256": row["source_sha256"] or "",
                "source_hash_algorithm": row["source_hash_algorithm"] or "",
                "indexed_source_sha256": row["indexed_source_sha256"] or "",
                "fingerprint_version": row["fingerprint_version"] or "",
                "text_fingerprint": row["text_fingerprint"] or "",
                "media_fingerprint": row["media_fingerprint"] or "",
                "structure_fingerprint": row["structure_fingerprint"] or "",
                "semantic_fingerprint": row["semantic_fingerprint"] or "",
                "design_fingerprint": row["design_fingerprint"] or "",
                "derivative_path": row["derivative_path"] or "",
                "derivative_sha256": row["derivative_sha256"] or "",
                "derivative_provenance": row["derivative_provenance"] or "",
                "conversion_method": row["conversion_method"] or "",
                "relations": "; ".join(
                    f"#{item['id']}:{item['relation_type']}:{item['related_path']}" for item in relations
                ),
                "error": _single_line(row["error"]),
            }
        )
    return output


def export_catalog_csv(database: IndexDatabase, destination: str | Path) -> dict[str, Any]:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = catalog_rows(database)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {"output": str(path), "rows": len(rows), "columns": len(CATALOG_FIELDS)}
