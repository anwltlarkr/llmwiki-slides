from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, load_config
from .conversion import convert_presentations
from .database import IndexDatabase
from .pipeline import export_all, run_qmd, run_qmd_search, run_update, watch
from .scanner import discover


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _config(args: argparse.Namespace) -> AppConfig:
    return load_config(
        args.config,
        vault_override=getattr(args, "vault", None),
        database_override=getattr(args, "database", None),
    )


def _progress(event: dict[str, Any]) -> None:
    name = event["event"]
    if name == "scan-complete":
        print(f"Discovered {event['files']} presentation files.", file=sys.stderr)
    elif name == "index-start":
        print(f"Indexing {event['queued']} files with {event['workers']} worker processes.", file=sys.stderr)
    elif name == "file-complete":
        completed = int(event["completed"])
        total = int(event["total"])
        if completed == 1 or completed == total or completed % 25 == 0 or event["status"] == "failed":
            print(f"[{completed}/{total}] {event['status']}: {event['path']}", file=sys.stderr)
    elif name == "watch-sleep":
        print(f"Watching for changes; next scan in {event['seconds']} seconds.", file=sys.stderr)
    elif name == "conversion-discovery":
        print(
            f"Conversion queue: {event['reusable']} reusable, {event['app_converted']} newly exported, "
            f"{event['missing']} still missing.",
            file=sys.stderr,
        )
    elif name == "conversion-index":
        completed = int(event["completed"])
        total = int(event["total"])
        if completed == 1 or completed == total or completed % 25 == 0:
            print(f"[{completed}/{total}] indexed converted source: {event['path']}", file=sys.stderr)
    elif name == "conversion-file" and event.get("status") == "failed":
        print(f"Conversion failed: {event['path']}", file=sys.stderr)


def _doctor(config: AppConfig) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "vault": {"path": str(config.vault), "exists": config.vault.exists()},
        "database_parent": {"path": str(config.database.parent), "writable": False},
        "scan_roots": [{"path": str(path), "exists": path.exists()} for path in config.scan_roots],
        "commands": {
            "qmd": shutil.which(config.qmd_command) or shutil.which("qmd"),
            "markitdown": shutil.which("markitdown"),
            "soffice": shutil.which("soffice"),
            "osascript": shutil.which("osascript"),
        },
        "keynote": Path("/Applications/Keynote.app").exists(),
        "powerpoint": Path("/Applications/Microsoft PowerPoint.app").exists(),
        "fts5": False,
    }
    try:
        config.database.parent.mkdir(parents=True, exist_ok=True)
        probe = config.database.parent / ".llmwiki-slides-write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        checks["database_parent"]["writable"] = True
    except OSError as exc:
        checks["database_parent"]["error"] = str(exc)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        checks["fts5"] = True
    except sqlite3.DatabaseError as exc:
        checks["fts5_error"] = str(exc)
    finally:
        connection.close()
    checks["ready"] = bool(
        checks["vault"]["exists"]
        and checks["database_parent"]["writable"]
        and checks["fts5"]
        and all(item["exists"] for item in checks["scan_roots"])
    )
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmwiki-slides",
        description="Parallel presentation indexing for local Markdown LLM wikis.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument("--vault", help="Override the configured vault path")
    parser.add_argument("--database", help="Override the configured SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create the database and Markdown output directories")
    commands.add_parser("doctor", help="Check local indexing capabilities")

    scan = commands.add_parser("scan", help="Discover presentation files without indexing them")
    scan.add_argument("--json", action="store_true", help="Print the full inventory as JSON")

    update = commands.add_parser("update", help="Incrementally index new or changed presentations")
    update.add_argument("--workers", type=int, help="Number of extraction worker processes")
    update.add_argument("--force", action="store_true", help="Reindex files even when unchanged")
    update.add_argument("--limit", type=int, help="Process at most this many queued files")
    update.add_argument("--spotlight", action=argparse.BooleanOptionalAction, default=None)
    update.add_argument("--qmd", action=argparse.BooleanOptionalAction, default=None)

    search = commands.add_parser("search", help="Search indexed slide text, notes, metadata, and tags")
    search.add_argument("query")
    search.add_argument("-n", "--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.add_argument("--all-slides", action="store_true", help="Return every matching slide instead of one best slide per deck")

    commands.add_parser("status", help="Show index and job counts")
    commands.add_parser("export", help="Regenerate all Markdown source pages from SQLite")

    convert = commands.add_parser("convert", help="Index existing or newly exported Keynote/legacy PowerPoint derivatives")
    convert.add_argument("--reuse-existing", action=argparse.BooleanOptionalAction, default=True)
    convert.add_argument("--run-apps", action="store_true", help="Serially run Keynote or PowerPoint for missing derivatives")
    convert.add_argument("--limit", type=int, help="Inspect at most this many conversion candidates")
    convert.add_argument("--workers", type=int, help="Parallel workers used after serialized conversion")

    qmd = commands.add_parser("qmd", help="Update qmd after Markdown export")
    qmd.add_argument("--embed", action="store_true", help="Also refresh semantic embeddings")

    qmd_search = commands.add_parser("qmd-search", help="Search generated Markdown through the configured qmd index")
    qmd_search.add_argument("query")
    qmd_search.add_argument("-n", "--limit", type=int, default=20)
    qmd_search.add_argument("--json", action="store_true")

    watcher = commands.add_parser("watch", help="Continuously scan and index in the background")
    watcher.add_argument("--interval", type=float, default=60.0)
    watcher.add_argument("--workers", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config(args)
        if args.command == "init":
            config.ensure_directories()
            with IndexDatabase(config.database) as database:
                database.initialize()
            _json({
                "database": str(config.database),
                "markdown_output": str(config.markdown_output),
                "status": "initialized",
            })
        elif args.command == "doctor":
            report = _doctor(config)
            _json(report)
            return 0 if report["ready"] else 1
        elif args.command == "scan":
            candidates = discover(config)
            if args.json:
                _json([candidate.to_dict() for candidate in candidates])
            else:
                counts: dict[str, int] = {}
                for candidate in candidates:
                    counts[candidate.extension] = counts.get(candidate.extension, 0) + 1
                _json({
                    "files": len(candidates),
                    "by_extension": dict(sorted(counts.items())),
                    "roots": [str(path) for path in config.scan_roots],
                })
        elif args.command == "update":
            summary = run_update(
                config,
                workers=args.workers,
                force=args.force,
                limit=args.limit,
                spotlight=args.spotlight,
                update_qmd=args.qmd,
                progress=_progress,
            )
            _json(summary)
            return 1 if summary.get("failed") else 0
        elif args.command == "search":
            with IndexDatabase(config.database, readonly=True) as database:
                results = database.search(
                    args.query,
                    limit=max(1, args.limit),
                    all_slides=args.all_slides,
                )
            if args.json:
                _json(results)
            else:
                if not results:
                    print("No matches.")
                for index, result in enumerate(results, start=1):
                    location = result["path"]
                    if result["slide_number"] is not None:
                        location += f"#slide-{result['slide_number']}"
                    print(f"{index}. {result['filename']} [{result['status']}]")
                    print(f"   {location}")
                    if result["slide_title"]:
                        print(f"   {result['slide_title']}")
                    if result["snippet"]:
                        print(f"   {result['snippet']}")
        elif args.command == "status":
            with IndexDatabase(config.database) as database:
                database.initialize()
                _json(database.status())
        elif args.command == "export":
            _json(export_all(config))
        elif args.command == "convert":
            summary = convert_presentations(
                config,
                reuse_existing=args.reuse_existing,
                run_apps=args.run_apps,
                limit=args.limit,
                workers=args.workers,
                progress=_progress,
            )
            _json(summary)
            return 1 if summary.get("conversion_failed") or summary.get("indexing_failed") else 0
        elif args.command == "qmd":
            qmd_results = run_qmd(config, embed=args.embed)
            _json(qmd_results)
            return 1 if any(item["returncode"] != 0 for item in qmd_results) else 0
        elif args.command == "qmd-search":
            qmd_results = run_qmd_search(config, args.query, limit=max(1, args.limit))
            if args.json:
                _json(qmd_results)
            else:
                if not qmd_results:
                    print("No matches.")
                for index, result in enumerate(qmd_results, start=1):
                    print(f"{index}. {result.get('title') or result.get('file')} [{result.get('score', '')}]")
                    print(f"   {result.get('file', '')}")
                    if result.get("snippet"):
                        print(f"   {result['snippet']}")
        elif args.command == "watch":
            try:
                watch(config, interval=max(1.0, args.interval), workers=args.workers, progress=_progress)
            except KeyboardInterrupt:
                print("Stopped.", file=sys.stderr)
        return 0
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.DatabaseError, OSError) as exc:
        print(f"llmwiki-slides: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
