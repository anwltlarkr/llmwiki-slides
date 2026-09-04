from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import AppConfig
from .database import IndexDatabase
from .exporter import export_markdown
from .extract import extract_candidate, init_worker
from .fingerprints import source_sha256


ProgressCallback = Callable[[dict[str, Any]], None]
MANIFEST_SUFFIX = ".llmwiki-slides.json"


@dataclass(slots=True)
class DerivativeMatch:
    path: Path
    method: str
    provenance: str
    source_sha256: str
    derivative_sha256: str


def _emit(callback: ProgressCallback | None, event: str, **values: Any) -> None:
    if callback:
        callback({"event": event, **values})


def _path_hash(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", errors="surrogateescape")).hexdigest()[:10]


def _reuse_roots(config: AppConfig) -> list[Path]:
    roots = list(config.conversion_roots)
    if config.conversion_cache and config.conversion_cache not in roots:
        roots.append(config.conversion_cache)
    return roots


def _manifest_path(derivative: Path) -> Path:
    return derivative.with_name(derivative.name + MANIFEST_SUFFIX)


def _write_manifest(
    derivative: Path,
    candidate: dict[str, Any],
    *,
    method: str,
    provenance: str,
    source_hash: str,
    source_algorithm: str,
    derivative_hash: str,
) -> Path:
    destination = _manifest_path(derivative)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_path": candidate["path"],
        "source_sha256": source_hash,
        "source_hash_algorithm": source_algorithm,
        "derivative_path": str(derivative),
        "derivative_sha256": derivative_hash,
        "conversion_method": method,
        "provenance": provenance,
        "llmwiki_slides_version": __version__,
    }
    descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def _validated_manifest(derivative: Path, source_hash: str) -> DerivativeMatch | None:
    manifest_path = _manifest_path(derivative)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("schema_version") != 1 or payload.get("source_sha256") != source_hash:
        return None
    derivative_hash, _ = source_sha256(derivative)
    if payload.get("derivative_sha256") != derivative_hash:
        return None
    return DerivativeMatch(
        path=derivative,
        method=str(payload.get("conversion_method") or "manifest-reuse"),
        provenance=str(payload.get("provenance") or "verified-manifest"),
        source_sha256=source_hash,
        derivative_sha256=derivative_hash,
    )


def audit_derivative_manifests(database: IndexDatabase) -> dict[str, int]:
    counts = {"verified": 0, "legacy_unverified": 0, "invalid_or_missing": 0}
    rows = database.connection.execute(
        """
        SELECT f.source_sha256, v.derivative_path, v.derivative_provenance
        FROM files f JOIN versions v ON v.id=f.current_version_id
        WHERE v.derivative_path<>''
        """
    ).fetchall()
    for row in rows:
        if row["derivative_provenance"] == "legacy-unverified":
            counts["legacy_unverified"] += 1
            continue
        derivative = Path(row["derivative_path"])
        if not row["source_sha256"] or not derivative.exists():
            counts["invalid_or_missing"] += 1
            continue
        if _validated_manifest(derivative, row["source_sha256"]):
            counts["verified"] += 1
        else:
            counts["invalid_or_missing"] += 1
    return counts


def find_reusable_derivative(
    config: AppConfig,
    candidate: dict[str, Any],
    *,
    adopt_legacy: bool = False,
) -> DerivativeMatch | None:
    source_hash, source_algorithm = source_sha256(candidate["path"])
    source_id = _path_hash(candidate["path"])
    inspected: set[Path] = set()
    for root in _reuse_roots(config):
        directory = root / source_id
        if not directory.exists():
            continue
        presentations = sorted(
            directory.glob("*.pptx"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for derivative in presentations:
            inspected.add(derivative.resolve())
            validated = _validated_manifest(derivative, source_hash)
            if validated:
                return validated
            if adopt_legacy:
                derivative_hash, _ = source_sha256(derivative)
                _write_manifest(
                    derivative,
                    candidate,
                    method="legacy-adopted",
                    provenance="user-adopted",
                    source_hash=source_hash,
                    source_algorithm=source_algorithm,
                    derivative_hash=derivative_hash,
                )
                return DerivativeMatch(
                    path=derivative,
                    method="legacy-adopted",
                    provenance="user-adopted",
                    source_sha256=source_hash,
                    derivative_sha256=derivative_hash,
                )
    for root in _reuse_roots(config):
        if not root.exists():
            continue
        for manifest in root.rglob(f"*{MANIFEST_SUFFIX}"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if payload.get("source_sha256") != source_hash:
                continue
            derivative = Path(str(manifest)[: -len(MANIFEST_SUFFIX)])
            if not derivative.exists() or derivative.resolve() in inspected:
                continue
            validated = _validated_manifest(derivative, source_hash)
            if validated:
                return validated
    return None


def _newest_pptx(directory: Path, before: dict[Path, tuple[int, int, str]], expected: Path) -> Path:
    candidates = sorted(directory.glob("*.pptx"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    changed = [
        path
        for path in candidates
        if path.resolve() not in before
        or before[path.resolve()] != (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            source_sha256(path)[0],
        )
    ]
    if changed:
        return expected if expected in changed else changed[0]
    raise FileNotFoundError("Application export finished without a PPTX derivative")


def run_app_conversion(config: AppConfig, candidate: dict[str, Any]) -> DerivativeMatch:
    if not config.conversion_cache:
        raise ValueError("conversion.cache is not configured")
    output_dir = config.conversion_cache / _path_hash(candidate["path"])
    output_dir.mkdir(parents=True, exist_ok=True)
    before = {
        path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns, source_sha256(path)[0])
        for path in output_dir.glob("*.pptx")
    }
    expected = output_dir / f"{Path(candidate['filename']).stem}.pptx"
    source_before, source_algorithm = source_sha256(candidate["path"])
    extension = candidate["extension"]
    if extension in {".key", ".keynote"}:
        resource = files("llmwiki_slides").joinpath("scripts/export_keynote.scpt")
        method = "keynote-export"
        with as_file(resource) as script:
            command = ["osascript", str(script), candidate["path"], str(output_dir), "pptx"]
            completed = subprocess.run(command, text=True, capture_output=True, timeout=config.conversion_timeout)
    elif extension in {".ppt", ".pps", ".pptx"}:
        resource = files("llmwiki_slides").joinpath("scripts/export_powerpoint.scpt")
        method = "powerpoint-export"
        with as_file(resource) as script:
            command = ["osascript", str(script), candidate["path"], str(output_dir)]
            completed = subprocess.run(command, text=True, capture_output=True, timeout=config.conversion_timeout)
    else:
        raise ValueError(f"No conversion adapter for {extension}")
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(message[:1000] or f"Conversion exited with {completed.returncode}")
    derivative = _newest_pptx(output_dir, before, expected)
    source_after, repeated_algorithm = source_sha256(candidate["path"])
    if source_before != source_after or source_algorithm != repeated_algorithm:
        raise RuntimeError("source changed while the presentation application was exporting it")
    derivative_hash, _ = source_sha256(derivative)
    _write_manifest(
        derivative,
        candidate,
        method=method,
        provenance="verified-export",
        source_hash=source_before,
        source_algorithm=source_algorithm,
        derivative_hash=derivative_hash,
    )
    return DerivativeMatch(
        path=derivative,
        method=method,
        provenance="verified-export",
        source_sha256=source_before,
        derivative_sha256=derivative_hash,
    )


def convert_presentations(
    config: AppConfig,
    *,
    reuse_existing: bool = True,
    run_apps: bool = False,
    limit: int | None = None,
    workers: int | None = None,
    adopt_legacy: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    config.ensure_directories()
    with IndexDatabase(config.database) as database:
        database.initialize()
        recovered = database.recover_interrupted_conversion_jobs()
        candidates = database.conversion_candidates()
        if limit is not None:
            candidates = candidates[: max(0, limit)]
        queued: list[dict[str, Any]] = []
        reused = 0
        converted = 0
        missing = 0
        conversion_failed = 0
        unverified_legacy = 0
        for number, candidate in enumerate(candidates, start=1):
            try:
                match = (
                    find_reusable_derivative(config, candidate, adopt_legacy=adopt_legacy)
                    if reuse_existing
                    else None
                )
                if match:
                    reused += 1
                elif run_apps:
                    match = run_app_conversion(config, candidate)
                    converted += 1
            except Exception as exc:
                conversion_failed += 1
                database.set_conversion_error(int(candidate["_file_id"]), f"{type(exc).__name__}: {exc}")
                _emit(
                    progress,
                    "conversion-file",
                    completed=number,
                    total=len(candidates),
                    status="failed",
                    path=candidate["path"],
                )
                continue
            if not match:
                missing += 1
                source_id = _path_hash(candidate["path"])
                if any(any((root / source_id).glob("*.pptx")) for root in _reuse_roots(config)):
                    unverified_legacy += 1
                continue
            queued.append(
                database.queue_conversion_import(
                    candidate,
                    match.path,
                    method=match.method,
                    source_sha256=match.source_sha256,
                    derivative_sha256=match.derivative_sha256,
                    provenance=match.provenance,
                )
            )

        _emit(
            progress,
            "conversion-discovery",
            candidates=len(candidates),
            reusable=reused,
            app_converted=converted,
            queued=len(queued),
            missing=missing,
        )
        indexed = 0
        indexing_failed = 0
        if queued:
            resolved_workers = max(1, workers or config.resolved_workers())
            context = mp.get_context(config.start_method)
            vocabulary = str(config.vocabulary) if config.vocabulary else None
            with ProcessPoolExecutor(
                max_workers=resolved_workers,
                mp_context=context,
                initializer=init_worker,
                initargs=(
                    vocabulary,
                    config.spotlight,
                    config.low_text_words_per_slide,
                    config.image_heavy_ratio,
                ),
            ) as pool:
                futures = {pool.submit(extract_candidate, item): item for item in queued}
                for number, future in enumerate(as_completed(futures), start=1):
                    item = futures[future]
                    try:
                        result = future.result()
                        file_id, _ = database.store_result(result, extractor_version=__version__)
                        if file_id is not None:
                            page_result = database.presentation_for_file(file_id) or result
                            page = export_markdown(config.markdown_output, page_result)
                            database.set_source_page(file_id, page)
                        if result.get("extraction_status") == "indexed-converted":
                            indexed += 1
                        else:
                            indexing_failed += 1
                    except Exception as exc:
                        indexing_failed += 1
                        database.record_writer_failure(item, f"{type(exc).__name__}: {exc}")
                    _emit(
                        progress,
                        "conversion-index",
                        completed=number,
                        total=len(queued),
                        path=item["path"],
                    )
        relations = database.rebuild_relations()
        relation_file_ids = relations.get("affected_file_ids", [])
        for file_id in relation_file_ids:
            page_result = database.presentation_for_file(file_id)
            if page_result is None:
                continue
            page = export_markdown(config.markdown_output, page_result)
            database.set_source_page(file_id, page)
        status = database.status()
    return {
        **status,
        "conversion_candidates": len(candidates),
        "recovered_interrupted": recovered,
        "reused": reused,
        "app_converted": converted,
        "missing_derivative": missing,
        "unverified_legacy_derivative": unverified_legacy,
        "conversion_failed": conversion_failed,
        "queued_for_index": len(queued),
        "indexed_converted": indexed,
        "indexing_failed": indexing_failed,
        "relations": {key: value for key, value in relations.items() if key != "affected_file_ids"},
    }
