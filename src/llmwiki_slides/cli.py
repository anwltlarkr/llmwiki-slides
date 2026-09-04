from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .catalog import export_catalog_csv
from .config import AppConfig, load_config
from .conversion import audit_derivative_manifests, convert_presentations
from .database import IndexDatabase
from .pipeline import export_all, run_integrity_backfill, run_qmd, run_qmd_search, run_update, watch
from .scanner import discover_with_report


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
    elif name == "integrity-file":
        completed = int(event["completed"])
        total = int(event["total"])
        if completed == 1 or completed == total or completed % 25 == 0 or event["status"] == "failed":
            print(f"[{completed}/{total}] {event['status']}: {event['path']}", file=sys.stderr)


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
    commands.add_parser("migrate", help="Back up and migrate an existing database to the current schema")
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
    export_csv = commands.add_parser("export-csv", help="Export the current presentation catalog as CSV")
    export_csv.add_argument("--output", help="CSV destination; defaults to the vault metadata directory")

    convert = commands.add_parser("convert", help="Index existing or newly exported Keynote/legacy PowerPoint derivatives")
    convert.add_argument("--reuse-existing", action=argparse.BooleanOptionalAction, default=True)
    convert.add_argument("--run-apps", action="store_true", help="Serially run Keynote or PowerPoint for missing derivatives")
    convert.add_argument("--limit", type=int, help="Inspect at most this many conversion candidates")
    convert.add_argument("--workers", type=int, help="Parallel workers used after serialized conversion")
    convert.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="Explicitly trust current source/derivative pairs that have no manifest",
    )

    integrity = commands.add_parser("integrity", help="Check and backfill archive identity metadata")
    integrity_commands = integrity.add_subparsers(dest="integrity_command", required=True)
    integrity_check = integrity_commands.add_parser("check", help="Run SQLite and identity metadata checks")
    integrity_check.add_argument("--strict", action="store_true", help="Fail when verification warnings remain")
    integrity_backfill = integrity_commands.add_parser(
        "backfill", help="Incrementally calculate exact source hashes and rebuild relations"
    )
    integrity_backfill.add_argument("--workers", type=int)
    integrity_backfill.add_argument("--limit", type=int)
    integrity_backfill.add_argument(
        "--verify-files",
        action="store_true",
        help="Re-read every present source even when its quick signature is unchanged",
    )
    integrity_relations = integrity_commands.add_parser("relations", help="List evidence-backed file relations")
    integrity_relations.add_argument("--needs-review", action="store_true")
    integrity_review = integrity_commands.add_parser("review", help="Confirm or reject a detected relation")
    integrity_review.add_argument("relation_id", type=int)
    integrity_review.add_argument("status", choices=("confirmed", "rejected", "unreviewed"))

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
        if args.command in {"init", "migrate"}:
            config.ensure_directories()
            with IndexDatabase(config.database) as database:
                database.initialize()
                backup = str(database.migration_backup_path or "")
            _json({
                "database": str(config.database),
                "markdown_output": str(config.markdown_output),
                "status": "migrated" if args.command == "migrate" else "initialized",
                "migration_backup": backup,
            })
        elif args.command == "doctor":
            report = _doctor(config)
            _json(report)
            return 0 if report["ready"] else 1
        elif args.command == "scan":
            report = discover_with_report(config)
            candidates = report.candidates
            if args.json:
                _json(
                    {
                        "complete": report.complete,
                        "roots": [root.to_dict() for root in report.roots],
                        "candidates": [candidate.to_dict() for candidate in candidates],
                    }
                )
            else:
                counts: dict[str, int] = {}
                for candidate in candidates:
                    counts[candidate.extension] = counts.get(candidate.extension, 0) + 1
                _json({
                    "files": len(candidates),
                    "by_extension": dict(sorted(counts.items())),
                    "roots": [str(path) for path in config.scan_roots],
                    "root_status": [root.to_dict() for root in report.roots],
                })
            return 0 if report.complete else 1
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
            return 1 if summary.get("failed") or summary.get("scan_status") != "complete" else 0
        elif args.command == "search":
            with IndexDatabase(config.database, readonly=True) as database:
                database.validate_schema()
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
                    print(
                        f"{index}. {result['filename']} "
                        f"[{result['status']}; {result['presence_status']}]"
                    )
                    print(f"   {location}")
                    if result["slide_title"]:
                        print(f"   {result['slide_title']}")
                    if result["snippet"]:
                        print(f"   {result['snippet']}")
        elif args.command == "status":
            with IndexDatabase(config.database, readonly=True) as database:
                database.validate_schema()
                _json(database.status())
        elif args.command == "export":
            _json(export_all(config))
        elif args.command == "export-csv":
            output = args.output or config.vault / "wiki/90-meta/llmwiki-slides/presentation-catalog.csv"
            with IndexDatabase(config.database, readonly=True) as database:
                database.validate_schema()
                _json(export_catalog_csv(database, output))
        elif args.command == "convert":
            summary = convert_presentations(
                config,
                reuse_existing=args.reuse_existing,
                run_apps=args.run_apps,
                limit=args.limit,
                workers=args.workers,
                adopt_legacy=args.adopt_legacy,
                progress=_progress,
            )
            _json(summary)
            return 1 if summary.get("conversion_failed") or summary.get("indexing_failed") else 0
        elif args.command == "integrity":
            if args.integrity_command == "check":
                with IndexDatabase(config.database, readonly=True) as database:
                    database.validate_schema()
                    report = database.integrity_report()
                    report["derivative_manifests"] = audit_derivative_manifests(database)
                    _json(report)
                    if report["derivative_manifests"]["invalid_or_missing"]:
                        return 1
                    return 1 if report["errors"] or (args.strict and report["warnings"]) else 0
            elif args.integrity_command == "backfill":
                report = run_integrity_backfill(
                    config,
                    workers=args.workers,
                    limit=args.limit,
                    verify_files=args.verify_files,
                    progress=_progress,
                )
                _json(report)
                return 1 if report.get("failed") or report["sqlite_integrity"] != "ok" else 0
            elif args.integrity_command == "relations":
                with IndexDatabase(config.database, readonly=True) as database:
                    database.validate_schema()
                    _json(database.relation_rows(needs_review=args.needs_review))
            elif args.integrity_command == "review":
                with IndexDatabase(config.database) as database:
                    database.initialize()
                    _json(database.set_relation_review(args.relation_id, args.status))
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
