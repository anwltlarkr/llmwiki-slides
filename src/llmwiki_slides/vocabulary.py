from __future__ import annotations

import re
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import default_vocabulary_path


FIELD_WEIGHTS = {
    "filename": 5.0,
    "path": 4.0,
    "title": 4.0,
    "slide_titles": 3.0,
    "notes": 1.5,
    "body": 1.0,
}

STOPWORDS = {
    "about", "after", "also", "analysis", "and", "are", "based", "been", "being",
    "between", "can", "data", "each", "figure", "figures", "for", "from", "have",
    "into", "more", "most", "not", "presentation", "result", "results", "slide", "slides",
    "study", "table", "tables", "that", "the", "their", "these", "this", "those", "through",
    "using", "was", "were", "which", "with", "및", "대한", "관련", "결과", "그림", "발표",
    "분석", "연구", "자료", "표",
}


def load_vocabulary(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else default_vocabulary_path()
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"Vocabulary rules must be an array: {target}")
    return rules


def _fields(presentation: dict[str, Any]) -> dict[str, str]:
    slides = presentation.get("slides", [])
    return {
        "filename": presentation["candidate"].get("filename", ""),
        "path": presentation["candidate"].get("path", ""),
        "title": presentation.get("title", ""),
        "slide_titles": "\n".join(slide.get("title", "") for slide in slides),
        "body": "\n".join(slide.get("body", "") for slide in slides),
        "notes": "\n".join(slide.get("notes", "") for slide in slides),
    }


def classify(presentation: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    fields = _fields(presentation)
    classified: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in rules:
        axis = str(rule.get("axis", "topic"))
        name = str(rule.get("name", "")).strip()
        patterns = [str(value) for value in rule.get("patterns", [])]
        selected_fields = [str(value) for value in rule.get("fields", FIELD_WEIGHTS)]
        if not name or not patterns:
            continue
        score = 0.0
        evidence: list[str] = []
        for field_name in selected_fields:
            text = fields.get(field_name, "")
            if not text:
                continue
            weight = FIELD_WEIGHTS.get(field_name, 1.0)
            for pattern in patterns:
                try:
                    matches = re.findall(pattern, text, flags=re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(f"Invalid vocabulary pattern {pattern!r} for {axis}/{name}: {exc}") from exc
                if matches:
                    count = min(3, len(matches))
                    score += weight * count
                    evidence.append(f"{field_name}:{pattern}")
        minimum = float(rule.get("min_score", 3.0))
        if score >= minimum:
            classified[axis].append({
                "name": name,
                "score": round(score, 2),
                "evidence": evidence[:12],
            })
    for matches in classified.values():
        matches.sort(key=lambda item: (-float(item["score"]), str(item["name"])))
    return dict(sorted(classified.items()))


def tokenize(text: str) -> list[str]:
    values = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}|[가-힣][가-힣A-Za-z0-9_+.-]{1,}", text.casefold())
    output: list[str] = []
    for value in values:
        cleaned = value.strip("._-+")
        if len(cleaned) < 3 or cleaned in STOPWORDS:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", cleaned):
            continue
        if re.fullmatch(r"[a-f0-9]{10,}", cleaned):
            continue
        output.append(cleaned)
    return output


def frequent_keywords(slides: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    document_frequency: Counter[str] = Counter()
    for slide in slides:
        text = "\n".join((slide.get("title", ""), slide.get("body", ""), slide.get("notes", "")))
        tokens = tokenize(text)
        counts.update(tokens)
        document_frequency.update(set(tokens))
    ranked = sorted(counts, key=lambda token: (-counts[token], -document_frequency[token], token))[:limit]
    return [
        {"term": token, "count": counts[token], "slide_count": document_frequency[token]}
        for token in ranked
    ]


def deterministic_summary(presentation: dict[str, Any]) -> str:
    slides = presentation.get("slides", [])
    keywords = presentation.get("keywords", [])
    classification = presentation.get("classification", {})
    labels = [
        hit["name"]
        for axis in ("project", "topic", "presentation_type", "output_relation", "event")
        for hit in classification.get(axis, [])[:2]
    ]
    titles = []
    for slide in slides:
        title = str(slide.get("title", "")).strip()
        if title and title not in titles:
            titles.append(title)
        if len(titles) == 4:
            break
    parts = [f"{len(slides)}-slide presentation"]
    if labels:
        parts.append("classified as " + ", ".join(labels[:5]))
    if titles:
        parts.append("covering " + "; ".join(titles))
    elif keywords:
        parts.append("with frequent terms " + ", ".join(item["term"] for item in keywords[:8]))
    return ", ".join(parts) + "."
