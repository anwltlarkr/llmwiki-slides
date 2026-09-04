from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any


DEFAULT_EXTENSIONS = (".pptx", ".ppsx", ".ppt", ".pps", ".key", ".keynote", ".odp")


@dataclass(slots=True)
class AppConfig:
    vault: Path
    database: Path
    markdown_output: Path
    scan_roots: list[Path]
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    exclude_fragments: tuple[str, ...] = ()
    vocabulary: Path | None = None
    extract_workers: int = 0
    scan_workers: int = 4
    start_method: str = "spawn"
    spotlight: bool = False
    qmd_command: str = "qmd"
    qmd_collection: str = "vault"
    qmd_cache_home: Path | None = None
    qmd_update_after_run: bool = False
    qmd_embed_after_run: bool = False
    low_text_words_per_slide: int = 18
    image_heavy_ratio: float = 1.2
    conversion_roots: list[Path] = field(default_factory=list)
    conversion_cache: Path | None = None
    conversion_timeout: int = 240
    config_path: Path | None = None

    def resolved_workers(self) -> int:
        if self.extract_workers > 0:
            return self.extract_workers
        cpu = os.cpu_count() or 2
        return max(1, min(6, cpu - 2))

    def ensure_directories(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.markdown_output.mkdir(parents=True, exist_ok=True)


def _resolve(base: Path, raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def default_vocabulary_path() -> Path:
    resource = files("llmwiki_slides").joinpath("default-vocabulary.toml")
    with as_file(resource) as path:
        return Path(path)


def load_config(
    path: str | Path | None = None,
    *,
    vault_override: str | Path | None = None,
    database_override: str | Path | None = None,
) -> AppConfig:
    config_path = Path(path).expanduser().resolve() if path else None
    data: dict[str, Any] = {}
    if config_path:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        base = config_path.parent
    else:
        base = Path.cwd()

    vault_data = _table(data, "vault")
    scan_data = _table(data, "scan")
    worker_data = _table(data, "workers")
    index_data = _table(data, "index")
    qmd_data = _table(data, "qmd")
    conversion_data = _table(data, "conversion")

    raw_vault = vault_override or vault_data.get("path") or base
    vault = _resolve(base, raw_vault)
    raw_db = database_override or vault_data.get("database") or "wiki/90-meta/llmwiki-slides/index.sqlite3"
    raw_markdown = vault_data.get("markdown_output") or "wiki/10-sources/presentations"
    database = _resolve(vault, raw_db)
    markdown_output = _resolve(vault, raw_markdown)

    raw_roots = scan_data.get("roots") or [str(Path.home() / "Desktop")]
    scan_roots = [_resolve(base, root) for root in raw_roots]
    raw_vocab = index_data.get("vocabulary")
    vocabulary = _resolve(base, raw_vocab) if raw_vocab else None

    extensions = tuple(
        value if str(value).startswith(".") else f".{value}"
        for value in scan_data.get("extensions", DEFAULT_EXTENSIONS)
    )
    exclude_fragments = tuple(str(value) for value in scan_data.get("exclude_fragments", ()))

    return AppConfig(
        vault=vault,
        database=database,
        markdown_output=markdown_output,
        scan_roots=scan_roots,
        extensions=tuple(ext.casefold() for ext in extensions),
        exclude_fragments=exclude_fragments,
        vocabulary=vocabulary,
        extract_workers=int(worker_data.get("extract", 0)),
        scan_workers=max(1, int(worker_data.get("scan", 4))),
        start_method=str(worker_data.get("start_method", "spawn")),
        spotlight=bool(index_data.get("spotlight", False)),
        qmd_command=str(qmd_data.get("command", shutil.which("qmd") or "qmd")),
        qmd_collection=str(qmd_data.get("collection", "vault")),
        qmd_update_after_run=bool(qmd_data.get("update_after_run", False)),
        qmd_embed_after_run=bool(qmd_data.get("embed_after_run", False)),
        low_text_words_per_slide=max(1, int(index_data.get("low_text_words_per_slide", 18))),
        image_heavy_ratio=max(0.0, float(index_data.get("image_heavy_ratio", 1.2))),
        conversion_roots=[_resolve(base, value) for value in conversion_data.get("reuse_roots", [])],
        conversion_cache=_resolve(vault, conversion_data.get("cache", "wiki/90-meta/llmwiki-slides/conversions")),
        conversion_timeout=max(30, int(conversion_data.get("timeout", 240))),
        config_path=config_path,
        qmd_cache_home=_resolve(base, qmd_data["cache_home"]) if qmd_data.get("cache_home") else None,
    )
