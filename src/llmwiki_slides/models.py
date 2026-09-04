from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Candidate:
    path: str
    filename: str
    extension: str
    size_bytes: int
    mtime_ns: int
    created_time: str
    modified_time: str
    quick_signature: str
    is_package: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Slide:
    number: int
    title: str
    body: str
    notes: str = ""
    word_count: int = 0
    image_count: int = 0
    table_count: int = 0
    chart_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Presentation:
    candidate: dict[str, Any]
    content_hash: str
    extraction_status: str
    derivative_path: str = ""
    conversion_method: str = ""
    title: str = ""
    creator: str = ""
    last_modified_by: str = ""
    internal_created: str = ""
    internal_modified: str = ""
    subject: str = ""
    source_keywords: str = ""
    spotlight_authors: str = ""
    spotlight_creator: str = ""
    spotlight_last_used: str = ""
    theme_colors: list[str] = field(default_factory=list)
    slides: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[dict[str, Any]] = field(default_factory=list)
    classification: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    summary: str = ""
    needs_visual_review: bool = False
    visual_review_reasons: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
