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
    "derivative_path",
    "conversion_method",
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
    rows = database.connection.execute(
        """
        SELECT f.path, f.source_page, f.filename, f.extension, f.status,
               f.size_bytes, f.created_time, f.modified_time, f.error,
               v.spotlight_last_used, v.title, v.creator, v.last_modified_by,
               v.slide_count, v.text_char_count, v.image_count,
               v.needs_visual_review, v.visual_reasons_json, v.theme_colors_json,
               v.classification_json, v.keywords_json, v.summary,
               v.derivative_path, v.conversion_method
        FROM files f
        LEFT JOIN versions v ON v.id=f.current_version_id
        ORDER BY f.path
        """
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        classification = _json(row["classification_json"], {})
        keywords = _json(row["keywords_json"], [])
        output.append(
            {
                "source_path": row["path"],
                "source_page": row["source_page"],
                "filename": row["filename"],
                "file_type": row["extension"].lstrip("."),
                "status": row["status"],
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
                "derivative_path": row["derivative_path"] or "",
                "conversion_method": row["conversion_method"] or "",
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
