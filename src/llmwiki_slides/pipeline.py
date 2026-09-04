from __future__ import annotations

import multiprocessing as mp
import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import AppConfig
from .database import IndexDatabase
from .exporter import export_markdown
from .extract import extract_candidate, init_worker
from .fingerprints import SourceChangedDuringHash, package_quick_metadata, source_sha256
from .scanner import discover_with_report


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(callback: ProgressCallback | None, event: str, **values: Any) -> None:
    if callback:
        callback({"event": event, **values})


def _index_profile(config: AppConfig) -> str:
    digest = hashlib.sha256()
    digest.update(__version__.encode())
    digest.update(str(config.low_text_words_per_slide).encode())
    digest.update(str(config.image_heavy_ratio).encode())
    if config.vocabulary and config.vocabulary.exists():
        digest.update(config.vocabulary.read_bytes())
    return digest.hexdigest()[:16]


def _scan_profile(config: AppConfig) -> str:
    payload = {
        "roots": [str(path.resolve(strict=False)) for path in config.scan_roots],
        "extensions": sorted(config.extensions),
        "exclude_fragments": sorted(config.exclude_fragments),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _hash_backfill_item(item: dict[str, Any]) -> dict[str, Any]:
    path = Path(item["path"])
    value, algorithm = source_sha256(path)
    if path.is_dir():
        _, _, current_signature = package_quick_metadata(path)
    else:
        stat = path.stat()
        current_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
    stored_signature = str(item["quick_signature"])
    if stored_signature != current_signature and not stored_signature.startswith(current_signature + ":"):
        raise SourceChangedDuringHash(f"source stat signature changed since scan: {path}")
    return {
        "id": int(item["id"]),
        "path": str(path),
        "sha256": value,
        "algorithm": algorithm,
        "quick_signature": stored_signature,
    }


def _qmd_environment(config: AppConfig) -> dict[str, str]:
    environment = os.environ.copy()
    command_path = Path(config.qmd_command).expanduser()
    sibling_node = command_path.parent / "node"
    if command_path.is_absolute() and sibling_node.exists():
        environment["PATH"] = str(command_path.parent) + os.pathsep + environment.get("PATH", "")
    if config.qmd_cache_home:
        config.qmd_cache_home.mkdir(parents=True, exist_ok=True)
        environment["XDG_CACHE_HOME"] = str(config.qmd_cache_home)
    if config.qmd_config_home:
        config.qmd_config_home.mkdir(parents=True, exist_ok=True)
        environment["XDG_CONFIG_HOME"] = str(config.qmd_config_home)
    return environment


def run_qmd_search(config: AppConfig, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [config.qmd_command, "search", query, "-c", config.qmd_collection, "--json", "-n", str(limit)],
        text=True,
        capture_output=True,
        timeout=120,
        env=_qmd_environment(config),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"qmd search exited with {completed.returncode}")
    import json

    return json.loads(completed.stdout)


def run_qmd(config: AppConfig, *, embed: bool | None = None) -> list[dict[str, Any]]:
    commands = [[config.qmd_command, "update"]]
    if embed if embed is not None else config.qmd_embed_after_run:
        commands.append([config.qmd_command, "embed"])
    results = []
    for command in commands:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=3600,
            env=_qmd_environment(config),
        )
        results.append({
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        })
        if completed.returncode != 0:
            break
    return results


def run_update(
    config: AppConfig,
    *,
    workers: int | None = None,
    force: bool = False,
    limit: int | None = None,
    spotlight: bool | None = None,
    update_qmd: bool | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    config.ensure_directories()
    scan_report = discover_with_report(config)
    candidates = scan_report.candidates
    profile = _index_profile(config)
    for candidate in candidates:
        candidate.quick_signature = f"{candidate.quick_signature}:{profile}"
    _emit(progress, "scan-complete", files=len(candidates), roots=[str(path) for path in config.scan_roots])

    if scan_report.complete:
        scan_status = "complete"
    elif scan_report.candidates or any(root.status == "complete" for root in scan_report.roots):
        scan_status = "partial"
    else:
        scan_status = "failed"

    with IndexDatabase(config.database) as database:
        database.initialize()
        scan_id = database.record_scan(scan_report, config_fingerprint=_scan_profile(config))
        try:
            work = database.register_candidates(
                candidates,
                force=force,
                scan_id=scan_id,
                queue_limit=limit,
            )
            if scan_report.complete:
                presence = database.reconcile_presence(
                    scan_id=scan_id,
                    seen_paths=(candidate.path for candidate in candidates),
                    complete_roots=(root.root for root in scan_report.roots),
                    extensions=config.extensions,
                    exclude_fragments=config.exclude_fragments,
                )
            else:
                presence = {"marked_missing": 0, "file_ids": [], "skipped": "scan not complete"}
            database.finish_scan(scan_id, scan_status)
        except Exception:
            database.finish_scan(scan_id, "failed")
            raise
        touched_file_ids = set(database.presence_changed_file_ids(scan_id))
        queued_total = max(len(work), database.pending_index_count())
        if not work:
            processed_summary = {"processed": 0, "failed": 0}
        else:
            database.mark_running(item["_job_id"] for item in work)
            resolved_workers = max(1, workers or config.resolved_workers())
            vocabulary = str(config.vocabulary) if config.vocabulary else None
            use_spotlight = config.spotlight if spotlight is None else spotlight
            context = mp.get_context(config.start_method)
            succeeded = 0
            failed = 0
            needs_conversion = 0
            _emit(progress, "index-start", queued=len(work), workers=resolved_workers)
            with ProcessPoolExecutor(
                max_workers=resolved_workers,
                mp_context=context,
                initializer=init_worker,
                initargs=(
                    vocabulary,
                    use_spotlight,
                    config.low_text_words_per_slide,
                    config.image_heavy_ratio,
                ),
            ) as pool:
                futures = {pool.submit(extract_candidate, item): item for item in work}
                for completed, future in enumerate(as_completed(futures), start=1):
                    item = futures[future]
                    try:
                        result = future.result()
                        file_id, _ = database.store_result(result, extractor_version=__version__)
                        if file_id is not None:
                            touched_file_ids.add(file_id)
                        status = result.get("extraction_status", "failed")
                        if status == "failed":
                            touched_file_ids.add(int(item["_file_id"]))
                            failed += 1
                        elif status == "needs-conversion":
                            needs_conversion += 1
                            succeeded += 1
                        else:
                            succeeded += 1
                        _emit(
                            progress,
                            "file-complete",
                            completed=completed,
                            total=len(work),
                            status=status,
                            path=item["path"],
                        )
                    except Exception as exc:
                        failed += 1
                        error = f"{type(exc).__name__}: {exc}"
                        database.record_writer_failure(item, error)
                        _emit(
                            progress,
                            "file-complete",
                            completed=completed,
                            total=len(work),
                            status="failed",
                            path=item["path"],
                            error=error,
                        )
            processed_summary = {
                "discovered": len(candidates),
                "queued": queued_total,
                "processed": len(work),
                "succeeded": succeeded,
                "failed": failed,
                "needs_conversion_processed": needs_conversion,
                "workers": resolved_workers,
                "index_profile": profile,
            }

        relations = database.rebuild_relations()
        touched_file_ids.update(relations.pop("affected_file_ids"))
        exported = 0
        for file_id in sorted(touched_file_ids):
            result = database.presentation_for_file(file_id)
            if result is None:
                continue
            page = export_markdown(config.markdown_output, result)
            database.set_source_page(file_id, page)
            exported += 1
        summary = database.status()
        summary.update(processed_summary)
        summary.update({
            "discovered": len(candidates),
            "queued": queued_total,
            "index_profile": profile,
            "scan_id": scan_id,
            "scan_status": scan_status,
            "scan_roots": [root.to_dict() for root in scan_report.roots],
            "presence_reconciliation": presence,
            "relation_refresh": relations,
            "exported": exported,
        })

    should_update_qmd = config.qmd_update_after_run if update_qmd is None else update_qmd
    if should_update_qmd:
        summary["qmd"] = run_qmd(config)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    _emit(progress, "update-complete", **summary)
    return summary


def export_all(config: AppConfig) -> dict[str, Any]:
    config.ensure_directories()
    exported = 0
    with IndexDatabase(config.database) as database:
        database.initialize()
        for file_id, result in database.current_presentations():
            page = export_markdown(config.markdown_output, result)
            database.set_source_page(file_id, page)
            exported += 1
    return {"exported": exported, "markdown_output": str(config.markdown_output)}


def run_integrity_backfill(
    config: AppConfig,
    *,
    workers: int | None = None,
    limit: int | None = None,
    verify_files: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    config.ensure_directories()
    with IndexDatabase(config.database) as database:
        database.initialize()
        work = database.hash_backfill_candidates(verify_files=verify_files, limit=limit)
        resolved_workers = max(1, workers or config.resolved_workers())
        succeeded = 0
        failed = 0
        touched: set[int] = set()
        if work:
            context = mp.get_context(config.start_method)
            with ProcessPoolExecutor(max_workers=resolved_workers, mp_context=context) as pool:
                futures = {pool.submit(_hash_backfill_item, item): item for item in work}
                for completed, future in enumerate(as_completed(futures), start=1):
                    item = futures[future]
                    try:
                        result = future.result()
                        database.update_file_hash(
                            result["id"], result["sha256"], result["algorithm"], result["quick_signature"]
                        )
                        touched.add(result["id"])
                        succeeded += 1
                        _emit(
                            progress,
                            "integrity-file",
                            completed=completed,
                            total=len(work),
                            status="hashed",
                            path=result["path"],
                        )
                    except Exception as exc:
                        failed += 1
                        _emit(
                            progress,
                            "integrity-file",
                            completed=completed,
                            total=len(work),
                            status="failed",
                            path=item["path"],
                            error=f"{type(exc).__name__}: {exc}",
                        )
        relations = database.rebuild_relations()
        touched.update(relations.pop("affected_file_ids"))
        exported = 0
        for file_id in sorted(touched):
            result = database.presentation_for_file(file_id)
            if result is None:
                continue
            page = export_markdown(config.markdown_output, result)
            database.set_source_page(file_id, page)
            exported += 1
        report = database.integrity_report()
        report.update(
            {
                "queued": len(work),
                "hashed": succeeded,
                "failed": failed,
                "workers": resolved_workers,
                "relations": relations,
                "exported": exported,
            }
        )
        return report


def watch(
    config: AppConfig,
    *,
    interval: float,
    workers: int | None,
    progress: ProgressCallback | None,
) -> None:
    while True:
        run_update(config, workers=workers, progress=progress)
        _emit(progress, "watch-sleep", seconds=interval)
        time.sleep(interval)
