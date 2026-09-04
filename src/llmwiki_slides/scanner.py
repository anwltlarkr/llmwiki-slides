from __future__ import annotations

import datetime as dt
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig
from .fingerprints import package_quick_metadata
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


@dataclass(slots=True)
class RootScanResult:
    root: str
    status: str
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "status": self.status,
            "candidate_count": len(self.candidates),
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class ScanReport:
    candidates: list[Candidate]
    roots: list[RootScanResult]

    @property
    def complete(self) -> bool:
        return bool(self.roots) and all(root.status == "complete" for root in self.roots)


def _iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _excluded(path: Path, fragments: tuple[str, ...]) -> bool:
    value = str(path)
    return any(fragment and fragment in value for fragment in fragments)


def _candidate(path: Path, *, is_package: bool = False) -> Candidate:
    try:
        stat = path.stat()
    except (FileNotFoundError, PermissionError, OSError):
        raise
    created = getattr(stat, "st_birthtime", stat.st_ctime)
    if is_package:
        size_bytes, mtime_ns, quick_signature = package_quick_metadata(path)
        modified = mtime_ns / 1_000_000_000
    else:
        size_bytes = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
        quick_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        modified = stat.st_mtime
    return Candidate(
        path=str(path.resolve()),
        filename=path.name,
        extension=path.suffix.casefold(),
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        created_time=_iso(created),
        modified_time=_iso(modified),
        quick_signature=quick_signature,
        is_package=is_package,
    )


def _scan_root(root: Path, config: AppConfig) -> RootScanResult:
    results: list[Candidate] = []
    errors: list[str] = []
    resolved = str(root.expanduser().resolve(strict=False))
    if not root.exists():
        return RootScanResult(resolved, "failed", errors=["scan root does not exist"])
    if _excluded(root, config.exclude_fragments):
        return RootScanResult(resolved, "failed", errors=["scan root is excluded by configuration"])
    if root.is_file():
        if root.suffix.casefold() in config.extensions and not root.name.startswith(("~$", "._")):
            try:
                results.append(_candidate(root))
            except OSError as exc:
                errors.append(f"{root}: {type(exc).__name__}: {exc}")
        return RootScanResult(resolved, "failed" if errors else "complete", results, errors)

    def on_error(error: OSError) -> None:
        errors.append(f"{getattr(error, 'filename', root)}: {type(error).__name__}: {error}")

    for directory, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
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
                try:
                    results.append(_candidate(child, is_package=True))
                except OSError as exc:
                    errors.append(f"{child}: {type(exc).__name__}: {exc}")
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            if filename.startswith(("~$", "._")):
                continue
            path = current / filename
            if path.suffix.casefold() not in config.extensions or _excluded(path, config.exclude_fragments):
                continue
            try:
                results.append(_candidate(path))
            except OSError as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return RootScanResult(resolved, "partial" if errors else "complete", results, errors)


def discover_with_report(config: AppConfig) -> ScanReport:
    workers = min(config.scan_workers, max(1, len(config.scan_roots)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="slide-scan") as pool:
        roots = list(pool.map(lambda root: _scan_root(root, config), config.scan_roots))
    unique: dict[str, Candidate] = {}
    for root in roots:
        for item in root.candidates:
            unique[item.path] = item
    return ScanReport(
        candidates=sorted(unique.values(), key=lambda item: item.path.casefold()),
        roots=roots,
    )


def discover(config: AppConfig) -> list[Candidate]:
    return discover_with_report(config).candidates
