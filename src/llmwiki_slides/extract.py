from __future__ import annotations

import hashlib
import os
import plistlib
import posixpath
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .models import Presentation, Slide
from .vocabulary import classify, deterministic_summary, frequent_keywords, load_vocabulary, tokenize


PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ODP_NS = {
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}

_WORKER_RULES: list[dict[str, Any]] | None = None
_WORKER_SPOTLIGHT = False
_LOW_TEXT_WORDS = 18
_IMAGE_HEAVY_RATIO = 1.2


def init_worker(
    vocabulary_path: str | None,
    spotlight: bool,
    low_text_words_per_slide: int,
    image_heavy_ratio: float,
) -> None:
    global _WORKER_RULES, _WORKER_SPOTLIGHT, _LOW_TEXT_WORDS, _IMAGE_HEAVY_RATIO
    _WORKER_RULES = load_vocabulary(vocabulary_path)
    _WORKER_SPOTLIGHT = spotlight
    _LOW_TEXT_WORDS = low_text_words_per_slide
    _IMAGE_HEAVY_RATIO = image_heavy_ratio


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_dir():
        for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: str(item)):
            digest.update(str(child.relative_to(path)).encode("utf-8", errors="surrogateescape"))
            try:
                stat = child.stat()
            except OSError:
                continue
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xml_texts(root: ET.Element) -> list[str]:
    return [
        element.text.strip()
        for element in root.iter()
        if element.tag.endswith("}t") and element.text and element.text.strip()
    ]


def _shape_title(root: ET.Element) -> str:
    for shape in root.findall(f".//{{{PML_NS}}}sp"):
        placeholder = shape.find(f".//{{{PML_NS}}}ph")
        placeholder_type = placeholder.attrib.get("type", "") if placeholder is not None else ""
        if placeholder_type not in {"title", "ctrTitle", "subTitle"}:
            continue
        values = _xml_texts(shape)
        if values:
            candidate = " ".join(values)
            if re.search(r"[A-Za-z가-힣]", candidate):
                return candidate
    return ""


def _slide_from_xml(number: int, raw: bytes, notes_raw: bytes | None = None) -> Slide:
    root = ET.fromstring(raw)
    all_text = _xml_texts(root)
    title = _shape_title(root)
    if not title and all_text:
        title = next(
            (value[:240] for value in all_text if re.search(r"[A-Za-z가-힣]", value)),
            all_text[0][:240],
        )
    body_values = list(all_text)
    if title:
        remaining = title
        while body_values and remaining:
            value = body_values[0]
            body_values.pop(0)
            remaining = remaining.removeprefix(value).strip()
            if remaining == title:
                break
    notes = ""
    if notes_raw:
        try:
            notes_values = _xml_texts(ET.fromstring(notes_raw))
            notes = "\n".join(value for value in notes_values if value != str(number))
        except ET.ParseError:
            notes = ""
    body = "\n".join(body_values)
    return Slide(
        number=number,
        title=title,
        body=body,
        notes=notes,
        word_count=len(tokenize("\n".join((title, body, notes)))),
        image_count=sum(1 for element in root.iter() if element.tag == f"{{{PML_NS}}}pic"),
        table_count=sum(1 for element in root.iter() if element.tag == f"{{{DML_NS}}}tbl"),
        chart_count=sum(1 for element in root.iter() if element.tag.endswith("}chart")),
    )


def _slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _related_notes_name(archive: zipfile.ZipFile, slide_name: str) -> str | None:
    rels_name = posixpath.join(
        posixpath.dirname(slide_name),
        "_rels",
        posixpath.basename(slide_name) + ".rels",
    )
    if rels_name not in archive.namelist():
        return None
    try:
        root = ET.fromstring(archive.read(rels_name))
    except ET.ParseError:
        return None
    for relationship in root.findall(f"{{{REL_NS}}}Relationship"):
        if relationship.attrib.get("Type", "").endswith("/notesSlide"):
            target = relationship.attrib.get("Target", "")
            return posixpath.normpath(posixpath.join(posixpath.dirname(slide_name), target))
    return None


def _core_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    output = {
        "title": "",
        "creator": "",
        "last_modified_by": "",
        "internal_created": "",
        "internal_modified": "",
        "subject": "",
        "source_keywords": "",
    }
    if "docProps/core.xml" not in archive.namelist():
        return output
    root = ET.fromstring(archive.read("docProps/core.xml"))
    namespace = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        "dcterms": "http://purl.org/dc/terms/",
    }
    paths = {
        "title": "dc:title",
        "creator": "dc:creator",
        "last_modified_by": "cp:lastModifiedBy",
        "internal_created": "dcterms:created",
        "internal_modified": "dcterms:modified",
        "subject": "dc:subject",
        "source_keywords": "cp:keywords",
    }
    for name, query in paths.items():
        element = root.find(query, namespace)
        if element is not None and element.text:
            output[name] = element.text.strip()
    return output


def _theme_colors(archive: zipfile.ZipFile) -> list[str]:
    names = [name for name in archive.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", name)]
    if not names:
        return []
    try:
        root = ET.fromstring(archive.read(sorted(names)[0]))
    except ET.ParseError:
        return []
    colors: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}srgbClr") and element.attrib.get("val"):
            value = "#" + element.attrib["val"].upper()
        elif element.tag.endswith("}sysClr") and element.attrib.get("lastClr"):
            value = "#" + element.attrib["lastClr"].upper()
        else:
            continue
        if value not in colors:
            colors.append(value)
    return colors[:16]


def _extract_pptx(path: Path) -> tuple[dict[str, str], list[dict[str, Any]], list[str]]:
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            (name for name in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)),
            key=_slide_sort_key,
        )
        slides = []
        for number, slide_name in enumerate(slide_names, start=1):
            notes_name = _related_notes_name(archive, slide_name)
            notes_raw = archive.read(notes_name) if notes_name and notes_name in archive.namelist() else None
            slides.append(_slide_from_xml(number, archive.read(slide_name), notes_raw).to_dict())
        return _core_properties(archive), slides, _theme_colors(archive)


def _extract_odp(path: Path) -> tuple[dict[str, str], list[dict[str, Any]], list[str]]:
    slides: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("content.xml"))
        for number, page in enumerate(root.findall(".//draw:page", ODP_NS), start=1):
            lines = ["".join(element.itertext()).strip() for element in page.findall(".//text:p", ODP_NS)]
            lines = [line for line in lines if line]
            title = lines[0] if lines else ""
            body = "\n".join(lines[1:])
            image_count = len(page.findall(".//draw:image", ODP_NS))
            slide = Slide(
                number=number,
                title=title,
                body=body,
                word_count=len(tokenize("\n".join(lines))),
                image_count=image_count,
            )
            slides.append(slide.to_dict())
    return {}, slides, []


def _spotlight(path: Path) -> dict[str, str]:
    attributes = ("kMDItemAuthors", "kMDItemCreator", "kMDItemLastUsedDate")
    args = ["mdls", "-plist", "-"]
    for attribute in attributes:
        args.extend(("-name", attribute))
    args.append(str(path))
    try:
        raw = subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=15)
        values = plistlib.loads(raw)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return {}

    def normalize(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return {
        "spotlight_authors": normalize(values.get("kMDItemAuthors")),
        "spotlight_creator": normalize(values.get("kMDItemCreator")),
        "spotlight_last_used": normalize(values.get("kMDItemLastUsedDate")),
    }


def _visual_review(slides: list[dict[str, Any]], low_text_words: int, image_heavy_ratio: float) -> tuple[bool, list[str]]:
    if not slides:
        return True, ["no slide-level text was extracted"]
    total_words = sum(int(slide.get("word_count", 0)) for slide in slides)
    total_images = sum(int(slide.get("image_count", 0)) for slide in slides)
    reasons = []
    if total_words / len(slides) < low_text_words:
        reasons.append(f"low text density ({total_words / len(slides):.1f} words/slide)")
    if total_images / len(slides) >= image_heavy_ratio:
        reasons.append(f"image-heavy deck ({total_images / len(slides):.1f} images/slide)")
    sparse_visual = sum(
        1 for slide in slides
        if int(slide.get("word_count", 0)) < 12 and int(slide.get("image_count", 0)) > 0
    )
    if sparse_visual / len(slides) >= 0.35:
        reasons.append(f"{sparse_visual} slides require visual interpretation")
    return bool(reasons), reasons


def extract_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(candidate["path"])
    path = Path(candidate.get("_extract_path") or source_path)
    if path == source_path:
        content_hash = _sha256(source_path)
    else:
        digest = hashlib.sha256()
        digest.update(_sha256(source_path).encode())
        digest.update(_sha256(path).encode())
        content_hash = digest.hexdigest()
    presentation = Presentation(
        candidate=candidate,
        content_hash=content_hash,
        extraction_status="needs-conversion",
        derivative_path=str(path) if path != source_path else "",
        conversion_method=str(candidate.get("_conversion_method", "")),
    )
    try:
        extraction_extension = path.suffix.casefold()
        if extraction_extension in {".pptx", ".ppsx"}:
            metadata, slides, colors = _extract_pptx(path)
        elif extraction_extension == ".odp":
            metadata, slides, colors = _extract_odp(path)
        else:
            if _WORKER_SPOTLIGHT and os.name == "posix":
                for key, value in _spotlight(path).items():
                    setattr(presentation, key, value)
            presentation.summary = "Presentation cataloged; conversion to PPTX or ODP is required for slide-level indexing."
            return presentation.to_dict()

        for key, value in metadata.items():
            setattr(presentation, key, value)
        presentation.slides = slides
        presentation.theme_colors = colors
        presentation.extraction_status = "indexed-converted" if path != source_path else "indexed"
        if _WORKER_SPOTLIGHT and os.name == "posix":
            for key, value in _spotlight(path).items():
                setattr(presentation, key, value)
        presentation.keywords = frequent_keywords(slides)
        rules = _WORKER_RULES if _WORKER_RULES is not None else load_vocabulary()
        payload = presentation.to_dict()
        presentation.classification = classify(payload, rules)
        needs_review, reasons = _visual_review(slides, _LOW_TEXT_WORDS, _IMAGE_HEAVY_RATIO)
        presentation.needs_visual_review = needs_review
        presentation.visual_review_reasons = reasons
        presentation.summary = deterministic_summary(presentation.to_dict())
        return presentation.to_dict()
    except zipfile.BadZipFile as exc:
        presentation.extraction_status = "needs-conversion"
        presentation.error = f"BadZipFile: {exc}"
        presentation.needs_visual_review = True
        presentation.visual_review_reasons = [
            "file is encrypted, mislabeled, or has a damaged ZIP container",
            "open and resave with PowerPoint or LibreOffice before slide-level indexing",
        ]
        presentation.summary = "Presentation cataloged, but its PPTX container requires recovery or conversion before indexing."
        return presentation.to_dict()
    except Exception as exc:
        presentation.extraction_status = "failed"
        presentation.error = f"{type(exc).__name__}: {exc}"
        presentation.summary = "Presentation extraction failed; source metadata remains cataloged."
        return presentation.to_dict()
