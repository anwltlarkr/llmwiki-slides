from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


SOURCE_FILE_ALGORITHM = "sha256-file-v1"
SOURCE_PACKAGE_ALGORITHM = "sha256-package-v1"
FINGERPRINT_VERSION = "1"
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


class SourceChangedDuringHash(RuntimeError):
    """Raised when a source changes while its digest is being calculated."""


def _frame(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _file_state(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _package_entries(path: Path) -> list[tuple[str, str, int, int]]:
    entries: list[tuple[str, str, int, int]] = []

    def on_error(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(path, followlinks=False, onerror=on_error):
        current = Path(directory)
        dirnames.sort()
        filenames.sort()
        for name in list(dirnames):
            child = current / name
            if not child.is_symlink():
                continue
            stat = child.lstat()
            relative = child.relative_to(path).as_posix()
            target = os.readlink(child)
            entries.append((relative, f"link:{target}", len(os.fsencode(target)), int(stat.st_mtime_ns)))
            dirnames.remove(name)
        for name in filenames:
            child = current / name
            stat = child.lstat()
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                target = os.readlink(child)
                entries.append((relative, f"link:{target}", len(os.fsencode(target)), int(stat.st_mtime_ns)))
            elif child.is_file():
                entries.append((relative, "file", int(stat.st_size), int(stat.st_mtime_ns)))
    return sorted(entries, key=lambda item: os.fsencode(item[0]))


def package_quick_metadata(path: Path) -> tuple[int, int, str]:
    """Return total bytes, newest mtime, and a metadata-only package signature."""

    root_stat = path.stat()
    entries = _package_entries(path)
    total_size = sum(size for _, kind, size, _ in entries if kind == "file")
    newest_mtime = max([int(root_stat.st_mtime_ns), *(mtime for *_, mtime in entries)])
    digest = hashlib.sha256(b"llmwiki-slides-package-quick-v1\0")
    for relative, kind, size, mtime_ns in entries:
        _frame(digest, os.fsencode(relative))
        _frame(digest, kind.encode("utf-8", errors="surrogateescape"))
        _frame(digest, str(size).encode())
        _frame(digest, str(mtime_ns).encode())
    return total_size, newest_mtime, f"package-v1:{digest.hexdigest()}"


def _hash_file_once(path: Path) -> str:
    before = _file_state(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if before != _file_state(path):
        raise SourceChangedDuringHash(f"source changed while hashing: {path}")
    return digest.hexdigest()


def _hash_package_once(path: Path) -> str:
    before = _package_entries(path)
    digest = hashlib.sha256(b"llmwiki-slides-package-v1\0")
    _frame(digest, str(len(before)).encode())
    for relative, kind, size, _ in before:
        child = path / relative
        _frame(digest, os.fsencode(relative))
        if kind.startswith("link:"):
            _frame(digest, b"link")
            _frame(digest, os.fsencode(kind.removeprefix("link:")))
            continue
        _frame(digest, b"file")
        _frame(digest, str(size).encode())
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    if before != _package_entries(path):
        raise SourceChangedDuringHash(f"package changed while hashing: {path}")
    return digest.hexdigest()


def source_sha256(path: str | Path, *, attempts: int = 2) -> tuple[str, str]:
    source = Path(path)
    algorithm = SOURCE_PACKAGE_ALGORITHM if source.is_dir() else SOURCE_FILE_ALGORITHM
    for attempt in range(max(1, attempts)):
        try:
            value = _hash_package_once(source) if source.is_dir() else _hash_file_once(source)
            return value, algorithm
        except SourceChangedDuringHash:
            if attempt + 1 >= max(1, attempts):
                raise
    raise AssertionError("unreachable")


def _digest_json(kind: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(f"llmwiki-slides-{kind}-v{FINGERPRINT_VERSION}\0".encode() + encoded).hexdigest()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t \f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _text_payload(slides: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "slide": int(slide.get("number", 0)),
            "title": normalize_text(slide.get("title")),
            "body": normalize_text(slide.get("body")),
            "notes": normalize_text(slide.get("notes")),
        }
        for slide in slides
    ]


def slide_text_fingerprint(slide: dict[str, Any]) -> str:
    return _digest_json("slide-text", _text_payload([slide])[0])


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def pptx_slide_names(archive: zipfile.ZipFile) -> list[str]:
    fallback = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda value: int(re.search(r"(\d+)", Path(value).stem).group(1)),
    )
    presentation_name = "ppt/presentation.xml"
    rels_name = "ppt/_rels/presentation.xml.rels"
    if presentation_name not in archive.namelist() or rels_name not in archive.namelist():
        return fallback
    try:
        relationships = ET.fromstring(archive.read(rels_name))
        targets = {
            item.attrib.get("Id", ""): posixpath.normpath(
                posixpath.join("ppt", item.attrib.get("Target", ""))
            )
            for item in relationships.findall(f"{{{RELATIONSHIP_NS}}}Relationship")
            if item.attrib.get("Type", "").endswith("/slide")
        }
        presentation = ET.fromstring(archive.read(presentation_name))
    except ET.ParseError:
        return fallback
    ordered: list[str] = []
    for slide_id in presentation.findall(f".//{{{PRESENTATION_NS}}}sldId"):
        relation_id = next(
            (value for key, value in slide_id.attrib.items() if _local_name(key) == "id" and value in targets),
            "",
        )
        target = targets.get(relation_id, "")
        if target in archive.namelist() and target not in ordered:
            ordered.append(target)
    ordered.extend(name for name in fallback if name not in ordered)
    return ordered


STRUCTURE_TAGS = {
    "sp",
    "pic",
    "graphicFrame",
    "cxnSp",
    "grpSp",
    "tbl",
    "chart",
    "oleObj",
    "videoFile",
    "audioFile",
    "timing",
}

DESIGN_TAGS = {
    "theme",
    "themeElements",
    "clrScheme",
    "fontScheme",
    "fmtScheme",
    "sldMaster",
    "sldLayout",
    "cSld",
    "bg",
    "bgPr",
    "sp",
    "pic",
    "graphicFrame",
    "grpSp",
    "xfrm",
    "off",
    "ext",
    "prstGeom",
    "custGeom",
    "solidFill",
    "gradFill",
    "pattFill",
    "noFill",
    "srgbClr",
    "schemeClr",
    "sysClr",
    "ln",
    "effectLst",
    "style",
    "fontRef",
    "rPr",
    "defRPr",
    "endParaRPr",
    "latin",
    "ea",
    "cs",
    "tblStyleId",
    "sldSz",
}


def _xml_tokens(raw: bytes, accepted: set[str]) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    output: list[dict[str, Any]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name not in accepted:
            continue
        output.append({
            "tag": name,
            "attrs": sorted((_local_name(key), value) for key, value in element.attrib.items()),
        })
    return output


def _media_entries(
    archive: zipfile.ZipFile, *, odp: bool
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if odp:
        names = [name for name in archive.namelist() if name.startswith("Pictures/") and not name.endswith("/")]
    else:
        names = [name for name in archive.namelist() if name.startswith("ppt/media/") and not name.endswith("/")]
    entries: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for name in names:
        raw = archive.read(name)
        item = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "extension": Path(name).suffix.casefold(),
        }
        entries.append(item)
        by_name[name] = item
    return (
        sorted(entries, key=lambda item: (item["sha256"], item["size"], item["extension"])),
        by_name,
    )


def _semantic_xml_tokens(raw: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    output: list[dict[str, Any]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name in {"f", "v", "t"} and element.text:
            output.append({"tag": name, "text": normalize_text(element.text)})
        elif name in {"ser", "pt"}:
            output.append(
                {
                    "tag": name,
                    "attrs": sorted(
                        (_local_name(key), value)
                        for key, value in element.attrib.items()
                        if _local_name(key) in {"idx", "order"}
                    ),
                }
            )
    return output


def _semantic_parts(
    archive: zipfile.ZipFile, *, odp: bool
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if odp:
        names = [name for name in archive.namelist() if name.startswith("ObjectReplacements/")]
    else:
        names = [
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/(charts|diagrams)/[^/]+\.xml", name)
            or name.startswith("ppt/embeddings/")
        ]
    output: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    design: list[dict[str, Any]] = []
    for name in sorted(names):
        if name.endswith("/"):
            continue
        raw = archive.read(name)
        if not odp and re.fullmatch(r"ppt/(charts|diagrams)/[^/]+\.xml", name):
            kind = name.split("/", 2)[1].rstrip("s")
            semantic_tokens = _semantic_xml_tokens(raw)
            item = {
                "type": kind,
                "sha256": _digest_json("part-semantic", semantic_tokens),
                "size": len(semantic_tokens),
            }
            design.append({"type": kind, "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            item = {
                "type": "embedded-object" if not odp else "object-replacement",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        output.append(item)
        by_name[name] = item
    output.sort(key=lambda item: (item["type"], item["sha256"], item["size"]))
    design.sort(key=lambda item: (item["type"], item["sha256"]))
    return output, by_name, design


def _pptx_linked_parts(
    archive: zipfile.ZipFile,
    slide_name: str,
    slide_raw: bytes,
    part_fingerprints: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rels_name = str(
        Path(slide_name).parent / "_rels" / (Path(slide_name).name + ".rels")
    ).replace("\\", "/")
    if rels_name not in archive.namelist():
        return []
    relationships = ET.fromstring(archive.read(rels_name))
    targets: dict[str, tuple[str, str]] = {}
    for relationship in relationships.findall(f"{{{RELATIONSHIP_NS}}}Relationship"):
        relation_id = relationship.attrib.get("Id", "")
        target = relationship.attrib.get("Target", "")
        relation_type = relationship.attrib.get("Type", "").rsplit("/", 1)[-1]
        if relationship.attrib.get("TargetMode") == "External":
            targets[relation_id] = (relation_type, f"external:{target}")
            continue
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(slide_name), target))
        targets[relation_id] = (relation_type, normalized)

    root = ET.fromstring(slide_raw)
    linked: list[dict[str, Any]] = []
    for element in root.iter():
        for attribute, relation_id in element.attrib.items():
            if _local_name(attribute) not in {"embed", "link", "id"} or relation_id not in targets:
                continue
            relation_type, target = targets[relation_id]
            item: dict[str, Any] = {"type": relation_type}
            if target.startswith("external:"):
                item["target"] = target
            elif target in archive.namelist() and not target.endswith("/"):
                fingerprint = part_fingerprints.get(target)
                if fingerprint is None:
                    raw = archive.read(target)
                    fingerprint = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                    part_fingerprints[target] = fingerprint
                item.update({"sha256": fingerprint["sha256"], "size": fingerprint["size"]})
            else:
                item["target"] = target
            linked.append(item)
    return linked


def _pptx_fingerprint_payloads(
    archive: zipfile.ZipFile, part_fingerprints: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, str]]]:
    slide_names = pptx_slide_names(archive)
    structure: list[dict[str, Any]] = []
    design: list[dict[str, Any]] = []
    per_slide: dict[int, dict[str, str]] = {}
    for number, name in enumerate(slide_names, start=1):
        raw = archive.read(name)
        counts = Counter(token["tag"] for token in _xml_tokens(raw, STRUCTURE_TAGS))
        structure_item = {
            "slide": number,
            "objects": sorted(counts.items()),
            "linked_parts": _pptx_linked_parts(archive, name, raw, part_fingerprints),
        }
        design_item = {"part": f"slide:{number}", "tokens": _xml_tokens(raw, DESIGN_TAGS)}
        structure.append(structure_item)
        design.append(design_item)
        per_slide[number] = {
            "structure_fingerprint": _digest_json("slide-structure", structure_item),
            "design_fingerprint": _digest_json("slide-design", design_item),
        }
    design_names = sorted(
        name
        for name in archive.namelist()
        if re.fullmatch(r"ppt/(theme/theme\d+|slideMasters/slideMaster\d+|slideLayouts/slideLayout\d+)\.xml", name)
        or name == "ppt/presentation.xml"
    )
    for name in design_names:
        design.append({"part": name, "tokens": _xml_tokens(archive.read(name), DESIGN_TAGS)})
    return structure, design, per_slide


def _odp_fingerprint_payloads(
    archive: zipfile.ZipFile, part_fingerprints: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, str]]]:
    root = ET.fromstring(archive.read("content.xml"))
    pages = [element for element in root.iter() if _local_name(element.tag) == "page"]
    structure: list[dict[str, Any]] = []
    design: list[dict[str, Any]] = []
    per_slide: dict[int, dict[str, str]] = {}
    for number, page in enumerate(pages, start=1):
        counts = Counter(_local_name(element.tag) for element in page.iter())
        linked_parts = []
        for element in page.iter():
            for attribute, target in element.attrib.items():
                if _local_name(attribute) != "href" or target not in archive.namelist():
                    continue
                fingerprint = part_fingerprints.get(target)
                if fingerprint is None:
                    raw = archive.read(target)
                    fingerprint = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                    part_fingerprints[target] = fingerprint
                linked_parts.append({"sha256": fingerprint["sha256"], "size": fingerprint["size"]})
        structure_item = {
            "slide": number,
            "objects": sorted((name, count) for name, count in counts.items() if name in {"frame", "image", "table", "object", "g"}),
            "linked_parts": linked_parts,
        }
        structure.append(structure_item)
        tokens = []
        for element in page.iter():
            name = _local_name(element.tag)
            if name not in {"page", "frame", "image", "table", "g"}:
                continue
            tokens.append({"tag": name, "attrs": sorted((_local_name(key), value) for key, value in element.attrib.items())})
        design_item = {"part": f"slide:{number}", "tokens": tokens}
        design.append(design_item)
        per_slide[number] = {
            "structure_fingerprint": _digest_json("slide-structure", structure_item),
            "design_fingerprint": _digest_json("slide-design", design_item),
        }
    for name in ("styles.xml", "settings.xml"):
        if name in archive.namelist():
            design.append({"part": name, "sha256": hashlib.sha256(archive.read(name)).hexdigest()})
    return structure, design, per_slide


def presentation_fingerprints(path: str | Path, slides: list[dict[str, Any]]) -> dict[str, Any]:
    presentation = Path(path)
    extension = presentation.suffix.casefold()
    with zipfile.ZipFile(presentation) as archive:
        is_odp = extension == ".odp"
        media_manifest, part_fingerprints = _media_entries(archive, odp=is_odp)
        semantic_parts, semantic_by_name, semantic_design = _semantic_parts(archive, odp=is_odp)
        part_fingerprints.update(semantic_by_name)
        if is_odp:
            structure, design, per_slide = _odp_fingerprint_payloads(archive, part_fingerprints)
        else:
            structure, design, per_slide = _pptx_fingerprint_payloads(archive, part_fingerprints)
        design.extend(semantic_design)
    text_payload = _text_payload(slides)
    text_fingerprint = _digest_json("text", text_payload)
    media_fingerprint = _digest_json("media", media_manifest)
    structure_fingerprint = _digest_json("structure", structure)
    for slide in slides:
        number = int(slide.get("number", 0))
        slide["text_fingerprint"] = slide_text_fingerprint(slide)
        slide.update(per_slide.get(number, {}))
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "text_fingerprint": text_fingerprint,
        "media_fingerprint": media_fingerprint,
        "media_manifest": media_manifest,
        "structure_fingerprint": structure_fingerprint,
        "semantic_fingerprint": _digest_json(
            "semantic",
            {
                "text": text_fingerprint,
                "media": media_fingerprint,
                "structure": structure_fingerprint,
                "parts": semantic_parts,
            },
        ),
        "design_fingerprint": _digest_json("design", design),
    }
