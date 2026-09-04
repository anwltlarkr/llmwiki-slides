from __future__ import annotations

import datetime as dt
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import AppConfig
from .models import Candidate


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".Trash",
    "Library",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
}


def _iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _excluded(path: Path, fragments: tuple[str, ...]) -> bool:
    value = str(path)
    return any(fragment and fragment in value for fragment in fragments)


def _candidate(path: Path, *, is_package: bool = False) -> Candidate | None:
    try:
        stat = path.stat()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    created = getattr(stat, "st_birthtime", stat.st_ctime)
    return Candidate(
        path=str(path.resolve()),
        filename=path.name,
        extension=path.suffix.casefold(),
        size_bytes=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        created_time=_iso(created),
        modified_time=_iso(stat.st_mtime),
        quick_signature=f"{stat.st_size}:{stat.st_mtime_ns}",
        is_package=is_package,
    )


def _scan_root(root: Path, config: AppConfig) -> list[Candidate]:
    results: list[Candidate] = []
    if not root.exists() or _excluded(root, config.exclude_fragments):
        return results
    if root.is_file():
        if root.suffix.casefold() in config.extensions and not root.name.startswith(("~$", "._")):
            item = _candidate(root)
            if item:
                results.append(item)
        return results

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        if _excluded(current, config.exclude_fragments):
            dirnames[:] = []
            continue

        kept_dirs: list[str] = []
        for dirname in dirnames:
            child = current / dirname
            extension = child.suffix.casefold()
            if dirname in DEFAULT_EXCLUDED_DIRS or _excluded(child, config.exclude_fragments):
                continue
            if extension in {".key", ".keynote"}:
                item = _candidate(child, is_package=True)
                if item:
                    results.append(item)
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            if filename.startswith(("~$", "._")):
                continue
            path = current / filename
            if path.suffix.casefold() not in config.extensions or _excluded(path, config.exclude_fragments):
                continue
            item = _candidate(path)
            if item:
                results.append(item)
    return results


def discover(config: AppConfig) -> list[Candidate]:
    workers = min(config.scan_workers, max(1, len(config.scan_roots)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="slide-scan") as pool:
        batches = list(pool.map(lambda root: _scan_root(root, config), config.scan_roots))
    unique: dict[str, Candidate] = {}
    for batch in batches:
        for item in batch:
            unique[item.path] = item
    return sorted(unique.values(), key=lambda item: item.path.casefold())
