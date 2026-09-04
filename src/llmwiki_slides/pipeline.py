from __future__ import annotations

import multiprocessing as mp
import hashlib
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
from .scanner import discover


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


def _qmd_environment(config: AppConfig) -> dict[str, str]:
    environment = os.environ.copy()
    command_path = Path(config.qmd_command).expanduser()
    sibling_node = command_path.parent / "node"
    if command_path.is_absolute() and sibling_node.exists():
        environment["PATH"] = str(command_path.parent) + os.pathsep + environment.get("PATH", "")
    if config.qmd_cache_home:
        config.qmd_cache_home.mkdir(parents=True, exist_ok=True)
        environment["XDG_CACHE_HOME"] = str(config.qmd_cache_home)
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
    candidates = discover(config)
    profile = _index_profile(config)
    for candidate in candidates:
        candidate.quick_signature = f"{candidate.quick_signature}:{profile}"
    _emit(progress, "scan-complete", files=len(candidates), roots=[str(path) for path in config.scan_roots])

    with IndexDatabase(config.database) as database:
        database.initialize()
        work = database.register_candidates(candidates, force=force)
        queued_total = len(work)
        if limit is not None:
            work = work[: max(0, limit)]
        if not work:
            summary = database.status()
            summary.update({"discovered": len(candidates), "queued": queued_total, "processed": 0, "failed": 0, "index_profile": profile})
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
                            page = export_markdown(config.markdown_output, result)
                            database.set_source_page(file_id, page)
                        status = result.get("extraction_status", "failed")
                        if status == "failed":
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
            summary = database.status()
            summary.update({
                "discovered": len(candidates),
                "queued": queued_total,
                "processed": len(work),
                "succeeded": succeeded,
                "failed": failed,
                "needs_conversion_processed": needs_conversion,
                "workers": resolved_workers,
                "index_profile": profile,
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
