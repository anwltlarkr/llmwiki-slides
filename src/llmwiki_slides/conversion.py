from __future__ import annotations

import hashlib
import multiprocessing as mp
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import AppConfig
from .database import IndexDatabase
from .exporter import export_markdown
from .extract import extract_candidate, init_worker


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(callback: ProgressCallback | None, event: str, **values: Any) -> None:
    if callback:
        callback({"event": event, **values})


def _path_hash(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", errors="surrogateescape")).hexdigest()[:10]


def find_reusable_derivative(config: AppConfig, candidate: dict[str, Any]) -> Path | None:
    source_id = _path_hash(candidate["path"])
    for root in config.conversion_roots:
        directory = root / source_id
        if not directory.exists():
            continue
        presentations = sorted(
            directory.glob("*.pptx"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if presentations:
            return presentations[0]
    return None


def _newest_pptx(directory: Path, before: set[Path], expected: Path) -> Path:
    candidates = sorted(directory.glob("*.pptx"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    new_files = [path for path in candidates if path.resolve() not in before]
    if new_files:
        return new_files[0]
    if expected.exists():
        return expected
    if candidates:
        return candidates[0]
    raise FileNotFoundError("Application export finished without a PPTX derivative")


def run_app_conversion(config: AppConfig, candidate: dict[str, Any]) -> tuple[Path, str]:
    if not config.conversion_cache:
        raise ValueError("conversion.cache is not configured")
    output_dir = config.conversion_cache / _path_hash(candidate["path"])
    output_dir.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in output_dir.glob("*.pptx")}
    expected = output_dir / f"{Path(candidate['filename']).stem}.pptx"
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
    return _newest_pptx(output_dir, before, expected), method


def convert_presentations(
    config: AppConfig,
    *,
    reuse_existing: bool = True,
    run_apps: bool = False,
    limit: int | None = None,
    workers: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    config.ensure_directories()
    with IndexDatabase(config.database) as database:
        database.initialize()
        candidates = database.conversion_candidates()
        if limit is not None:
            candidates = candidates[: max(0, limit)]
        queued: list[dict[str, Any]] = []
        reused = 0
        converted = 0
        missing = 0
        conversion_failed = 0
        for number, candidate in enumerate(candidates, start=1):
            derivative = find_reusable_derivative(config, candidate) if reuse_existing else None
            method = "reused-export"
            if derivative:
                reused += 1
            elif run_apps:
                try:
                    derivative, method = run_app_conversion(config, candidate)
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
            else:
                missing += 1
                continue
            job = database.queue_conversion_import(candidate, derivative, method=method)
            if job:
                queued.append(job)

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
                            page = export_markdown(config.markdown_output, result)
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
        status = database.status()
    return {
        **status,
        "conversion_candidates": len(candidates),
        "reused": reused,
        "app_converted": converted,
        "missing_derivative": missing,
        "conversion_failed": conversion_failed,
        "queued_for_index": len(queued),
        "indexed_converted": indexed,
        "indexing_failed": indexing_failed,
    }
