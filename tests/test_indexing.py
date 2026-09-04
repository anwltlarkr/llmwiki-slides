from __future__ import annotations

import tempfile
import unittest
import zipfile
import hashlib
import os
import sqlite3
from pathlib import Path

from llmwiki_slides.catalog import export_catalog_csv
from llmwiki_slides.database import IndexDatabase
from llmwiki_slides.config import AppConfig
from llmwiki_slides.conversion import find_reusable_derivative
from llmwiki_slides.exporter import export_markdown
from llmwiki_slides.extract import extract_candidate, init_worker
from llmwiki_slides.fingerprints import source_sha256
from llmwiki_slides.models import Candidate
from llmwiki_slides.scanner import RootScanResult, ScanReport
from llmwiki_slides.vocabulary import classify


SLIDE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Climate model overview</a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Regional simulations improve temperature projections across diverse climates at [[region:year]].</a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:pic><p:nvPicPr/><p:blipFill/><p:spPr/></p:pic>
  </p:spTree></p:cSld>
</p:sld>
"""

CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/">
 <dc:title>Research deck</dc:title><dc:creator>Test Author</dc:creator>
 <dcterms:created>2026-09-01T00:00:00Z</dcterms:created>
</cp:coreProperties>
"""

THEME_XML = """<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<a:themeElements><a:clrScheme name="Test"><a:accent1><a:srgbClr val="336699"/></a:accent1></a:clrScheme></a:themeElements>
</a:theme>"""

PRESENTATION_XML = """<p:presentation
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>"""

PRESENTATION_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{relationships}</Relationships>"""

CHART_SLIDE_XML = """<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:cSld><p:spTree><p:graphicFrame><a:graphic><a:graphicData>
 <c:chart r:id="rId1"/></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>"""

CHART_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
 Target="../charts/chart1.xml"/></Relationships>"""

CHART_XML = """<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:plotArea><c:barChart>
 <c:ser><c:idx val="0"/><c:order val="0"/><c:val><c:numRef><c:f>Sheet1!$A$1</c:f>
 <c:numCache><c:pt idx="0"><c:v>10</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser>
 </c:barChart></c:plotArea><c:spPr><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></c:spPr>
 </c:chart></c:chartSpace>"""


def make_pptx(path: Path, *, slide_xml: str = SLIDE_XML, theme_xml: str = THEME_XML) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
        archive.writestr("ppt/theme/theme1.xml", theme_xml)
        archive.writestr("docProps/core.xml", CORE_XML)


def make_ordered_pptx(path: Path, order: tuple[int, ...]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for number in (1, 2):
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                SLIDE_XML.replace("Climate model overview", f"Slide XML {number}"),
            )
        slide_ids = "".join(
            f'<p:sldId id="{255 + index}" r:id="rId{number}"/>'
            for index, number in enumerate(order, start=1)
        )
        relationships = "\n".join(
            f'<Relationship Id="rId{number}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{number}.xml"/>'
            for number in (1, 2)
        )
        archive.writestr("ppt/presentation.xml", PRESENTATION_XML.format(slide_ids=slide_ids))
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            PRESENTATION_RELS.format(relationships=relationships),
        )


def make_chart_pptx(path: Path, color: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", CHART_SLIDE_XML)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", CHART_RELS)
        archive.writestr("ppt/charts/chart1.xml", CHART_XML.format(color=color))


def candidate_for(path: Path) -> Candidate:
    stat = path.stat()
    return Candidate(
        path=str(path.resolve()),
        filename=path.name,
        extension=path.suffix,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        created_time="2026-09-01T00:00:00+09:00",
        modified_time="2026-09-01T00:00:00+09:00",
        quick_signature=f"{stat.st_size}:{stat.st_mtime_ns}",
    )


class IndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.deck = self.root / "climate-model-review.pptx"
        make_pptx(self.deck)
        init_worker(None, False, 18, 1.2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_extracts_slide_structure_and_metadata(self) -> None:
        result = extract_candidate(candidate_for(self.deck).to_dict())
        self.assertEqual(result["extraction_status"], "indexed")
        self.assertEqual(result["title"], "Research deck")
        self.assertEqual(result["creator"], "Test Author")
        self.assertEqual(result["slides"][0]["title"], "Climate model overview")
        self.assertIn("temperature projections", result["slides"][0]["body"])
        self.assertEqual(result["slides"][0]["image_count"], 1)
        self.assertIn("#336699", result["theme_colors"])
        self.assertEqual(len(result["source_sha256"]), 64)
        self.assertEqual(result["fingerprint_version"], "1")
        self.assertTrue(result["semantic_fingerprint"])
        self.assertTrue(result["design_fingerprint"])
        self.assertTrue(result["slides"][0]["text_fingerprint"])

    def test_weighted_vocabulary_uses_separate_axes(self) -> None:
        result = extract_candidate(candidate_for(self.deck).to_dict())
        rules = [
            {
                "axis": "project",
                "name": "climate-model",
                "patterns": [r"climate model", r"regional simulations?"],
                "fields": ["filename", "slide_titles", "body"],
                "min_score": 3,
            }
        ]
        classified = classify(result, rules)
        self.assertEqual(classified["project"][0]["name"], "climate-model")
        self.assertGreaterEqual(classified["project"][0]["score"], 3)

    def test_atomic_database_index_search_and_markdown_export(self) -> None:
        database_path = self.root / "index.sqlite3"
        markdown = self.root / "wiki"
        candidate = candidate_for(self.deck)
        with IndexDatabase(database_path) as database:
            database.initialize()
            work = database.register_candidates([candidate])
            self.assertEqual(len(work), 1)
            database.mark_running([work[0]["_job_id"]])
            result = extract_candidate(work[0])
            file_id, _ = database.store_result(result, extractor_version="test")
            self.assertIsNotNone(file_id)
            page = export_markdown(markdown, result)
            database.set_source_page(int(file_id), page)
            matches = database.search("temperature projections", limit=5)
            self.assertEqual(matches[0]["slide_number"], 1)
            self.assertIn("temperature", matches[0]["snippet"].casefold())
            catalog = self.root / "presentation-catalog.csv"
            report = export_catalog_csv(database, catalog)
            self.assertEqual(report["rows"], 1)
            catalog_text = catalog.read_text(encoding="utf-8")
            self.assertIn("source_path,source_page,filename,file_type,status", catalog_text)
            self.assertIn("climate-model-review.pptx", catalog_text)
            self.assertIn("climate-model", catalog_text)
            self.assertEqual(database.register_candidates([candidate]), [])
        text = page.read_text(encoding="utf-8")
        self.assertIn("type: source", text)
        self.assertIn("## Indexed Slide Text", text)
        self.assertIn("Regional simulations improve", text)
        self.assertIn(r"\[\[region:year\]\]", text)
        self.assertNotIn("[[region:year]]", text)

    def test_queued_job_is_resumable(self) -> None:
        database_path = self.root / "resume.sqlite3"
        candidate = candidate_for(self.deck)
        with IndexDatabase(database_path) as database:
            database.initialize()
            first = database.register_candidates([candidate])
            second = database.register_candidates([candidate])
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(first[0]["_job_id"], second[0]["_job_id"])

    def test_reuses_converted_derivative_for_original_keynote(self) -> None:
        original = self.root / "legacy.key"
        original.write_bytes(b"keynote-placeholder")
        source = candidate_for(original)
        source.extension = ".key"
        database_path = self.root / "converted.sqlite3"
        derivative_root = self.root / "exports"
        source_id = hashlib.sha1(source.path.encode()).hexdigest()[:10]
        derivative_dir = derivative_root / source_id
        derivative_dir.mkdir(parents=True)
        derivative = derivative_dir / "legacy.pptx"
        make_pptx(derivative)
        config = AppConfig(
            vault=self.root,
            database=database_path,
            markdown_output=self.root / "markdown",
            scan_roots=[self.root],
            conversion_roots=[derivative_root],
        )
        with IndexDatabase(database_path) as database:
            database.initialize()
            work = database.register_candidates([source])
            database.mark_running([work[0]["_job_id"]])
            catalog = extract_candidate(work[0])
            database.store_result(catalog, extractor_version="test")
            pending = database.conversion_candidates()[0]
            self.assertIsNone(find_reusable_derivative(config, pending))
            found = find_reusable_derivative(config, pending, adopt_legacy=True)
            self.assertEqual(found.path, derivative)
            self.assertEqual(found.provenance, "user-adopted")
            job = database.queue_conversion_import(
                pending,
                found.path,
                method=found.method,
                source_sha256=found.source_sha256,
                derivative_sha256=found.derivative_sha256,
                provenance=found.provenance,
            )
            self.assertIsNotNone(job)
            indexed = extract_candidate(job)
            self.assertEqual(indexed["extraction_status"], "indexed-converted")
            self.assertEqual(indexed["derivative_path"], str(derivative))
            database.store_result(indexed, extractor_version="test")
            self.assertEqual(database.conversion_candidates(), [])
            self.assertEqual(database.search("temperature projections")[0]["filename"], "legacy.key")

            refreshed = database.register_candidates([source], force=True)
            database.mark_running([refreshed[0]["_job_id"]])
            catalog = extract_candidate(refreshed[0])
            database.store_result(catalog, extractor_version="test")
            pending = database.conversion_candidates()[0]
            found = find_reusable_derivative(config, pending)
            repeated = database.queue_conversion_import(
                pending,
                found.path,
                method=found.method,
                source_sha256=found.source_sha256,
                derivative_sha256=found.derivative_sha256,
                provenance=found.provenance,
            )
            self.assertIsNotNone(repeated)
            indexed = extract_candidate(repeated)
            database.store_result(indexed, extractor_version="test")
            self.assertEqual(database.status()["file_status"], {"indexed-converted": 1})

            derivative.write_bytes(derivative.read_bytes() + b"tampered")
            self.assertIsNone(find_reusable_derivative(config, database.conversion_candidates()[0] if database.conversion_candidates() else pending))

    def test_style_only_change_is_a_design_variant(self) -> None:
        other = self.root / "climate-model-review-restyled.pptx"
        make_pptx(other, theme_xml=THEME_XML.replace("336699", "AA3300"))
        first = extract_candidate(candidate_for(self.deck).to_dict())
        second = extract_candidate(candidate_for(other).to_dict())
        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])
        self.assertNotEqual(first["design_fingerprint"], second["design_fingerprint"])

        database_path = self.root / "relations.sqlite3"
        with IndexDatabase(database_path) as database:
            database.initialize()
            for candidate in (candidate_for(self.deck), candidate_for(other)):
                work = database.register_candidates([candidate])
                database.mark_running([work[0]["_job_id"]])
                database.store_result(extract_candidate(work[0]), extractor_version="test")
            report = database.rebuild_relations()
            self.assertEqual(report["active"], {"design-variant": 1})
            relation = database.relation_rows()[0]
            self.assertEqual(relation["evidence"]["content_changed_slides"], [])
            self.assertEqual(relation["evidence"]["changed_design_slides"], [1])

    def test_text_change_is_not_a_format_or_design_variant(self) -> None:
        changed = self.root / "changed-text.pptx"
        make_pptx(changed, slide_xml=SLIDE_XML.replace("temperature projections", "rainfall projections"))
        first = extract_candidate(candidate_for(self.deck).to_dict())
        second = extract_candidate(candidate_for(changed).to_dict())
        self.assertNotEqual(first["text_fingerprint"], second["text_fingerprint"])
        self.assertNotEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])

    def test_damaged_refresh_retains_and_marks_prior_slide_index_stale(self) -> None:
        database_path = self.root / "damaged-refresh.sqlite3"
        with IndexDatabase(database_path) as database:
            database.initialize()
            work = database.register_candidates([candidate_for(self.deck)])
            database.mark_running([work[0]["_job_id"]])
            database.store_result(extract_candidate(work[0]), extractor_version="test")

            self.deck.write_bytes(self.deck.read_bytes()[:-20])
            refreshed = database.register_candidates([candidate_for(self.deck)])
            database.mark_running([refreshed[0]["_job_id"]])
            result = extract_candidate(refreshed[0])
            self.assertEqual(result["extraction_status"], "needs-conversion")
            file_id, _ = database.store_result(result, extractor_version="test")
            presentation = database.presentation_for_file(file_id)
            self.assertTrue(presentation["source_content_stale"])
            self.assertEqual(presentation["slides"][0]["title"], "Climate model overview")
            self.assertEqual(database.search("temperature projections")[0]["filename"], self.deck.name)

    def test_actual_presentation_order_controls_extraction_and_fingerprint(self) -> None:
        normal = self.root / "normal-order.pptx"
        reversed_deck = self.root / "reversed-order.pptx"
        make_ordered_pptx(normal, (1, 2))
        make_ordered_pptx(reversed_deck, (2, 1))
        first = extract_candidate(candidate_for(normal).to_dict())
        second = extract_candidate(candidate_for(reversed_deck).to_dict())
        self.assertEqual([slide["title"] for slide in first["slides"]], ["Slide XML 1", "Slide XML 2"])
        self.assertEqual([slide["title"] for slide in second["slides"]], ["Slide XML 2", "Slide XML 1"])
        self.assertNotEqual(first["text_fingerprint"], second["text_fingerprint"])
        self.assertNotEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])

    def test_chart_color_change_is_design_only(self) -> None:
        blue = self.root / "chart-blue.pptx"
        red = self.root / "chart-red.pptx"
        make_chart_pptx(blue, "336699")
        make_chart_pptx(red, "AA3300")
        first = extract_candidate(candidate_for(blue).to_dict())
        second = extract_candidate(candidate_for(red).to_dict())
        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])
        self.assertNotEqual(first["design_fingerprint"], second["design_fingerprint"])

    def test_keynote_package_hash_reads_nested_bytes(self) -> None:
        package = self.root / "nested.key"
        package.mkdir()
        child = package / "index.apxl"
        child.write_bytes(b"AAAA")
        original_stat = child.stat()
        first, algorithm = source_sha256(package)
        child.write_bytes(b"BBBB")
        os.utime(child, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        second, repeated_algorithm = source_sha256(package)
        self.assertEqual(algorithm, "sha256-package-v1")
        self.assertEqual(algorithm, repeated_algorithm)
        self.assertNotEqual(first, second)

    def test_partial_scan_never_marks_a_source_missing(self) -> None:
        database_path = self.root / "presence.sqlite3"
        candidate = candidate_for(self.deck)
        with IndexDatabase(database_path) as database:
            database.initialize()
            first_report = ScanReport([candidate], [RootScanResult(str(self.root), "complete", [candidate])])
            first_scan = database.record_scan(first_report, config_fingerprint="test")
            database.register_candidates([candidate], scan_id=first_scan)
            self.deck.unlink()
            partial_report = ScanReport([], [RootScanResult(str(self.root), "partial", errors=["denied"])])
            second_scan = database.record_scan(partial_report, config_fingerprint="test")
            result = database.reconcile_presence(
                scan_id=second_scan,
                seen_paths=[],
                complete_roots=[],
                extensions=[".pptx"],
                exclude_fragments=[],
            )
            self.assertEqual(result["marked_missing"], 0)
            self.assertEqual(database.status()["presence_status"], {"present": 1})

    def test_missing_plus_identical_new_path_is_only_a_possible_move(self) -> None:
        database_path = self.root / "move.sqlite3"
        original = candidate_for(self.deck)
        with IndexDatabase(database_path) as database:
            database.initialize()
            scan = database.record_scan(
                ScanReport([original], [RootScanResult(str(self.root), "complete", [original])]),
                config_fingerprint="test",
            )
            work = database.register_candidates([original], scan_id=scan)
            database.mark_running([work[0]["_job_id"]])
            database.store_result(extract_candidate(work[0]), extractor_version="test")

            moved_path = self.root / "renamed.pptx"
            self.deck.rename(moved_path)
            moved = candidate_for(moved_path)
            scan = database.record_scan(
                ScanReport([moved], [RootScanResult(str(self.root), "complete", [moved])]),
                config_fingerprint="test",
            )
            work = database.register_candidates([moved], scan_id=scan)
            database.reconcile_presence(
                scan_id=scan,
                seen_paths=[moved.path],
                complete_roots=[self.root],
                extensions=[".pptx"],
                exclude_fragments=[],
            )
            database.mark_running([work[0]["_job_id"]])
            database.store_result(extract_candidate(work[0]), extractor_version="test")
            database.rebuild_relations()
            relation = database.relation_rows()[0]
            self.assertEqual(relation["relation_type"], "possible-move")
            self.assertEqual(relation["evidence"]["identity_claim"], "byte equality only")
            database.set_relation_review(relation["id"], "confirmed")
            database.rebuild_relations()
            self.assertEqual(database.relation_rows()[0]["review_status"], "confirmed")

    def test_ambiguous_identical_group_never_claims_a_move(self) -> None:
        copies = [self.deck, self.root / "copy-one.pptx", self.root / "copy-two.pptx"]
        copies[1].write_bytes(self.deck.read_bytes())
        copies[2].write_bytes(self.deck.read_bytes())
        database_path = self.root / "ambiguous-move.sqlite3"
        with IndexDatabase(database_path) as database:
            database.initialize()
            file_ids = []
            for path in copies:
                work = database.register_candidates([candidate_for(path)])
                database.mark_running([work[0]["_job_id"]])
                file_id, _ = database.store_result(extract_candidate(work[0]), extractor_version="test")
                file_ids.append(file_id)
            database.connection.execute(
                "UPDATE files SET presence_status='missing' WHERE id=?", (file_ids[0],)
            )
            database.connection.commit()
            database.rebuild_relations()
            relations = database.relation_rows()
            self.assertEqual(len(relations), 3)
            self.assertEqual({item["relation_type"] for item in relations}, {"exact-duplicate"})
            self.assertTrue(
                any(item["evidence"]["move_candidate_ambiguous"] for item in relations)
            )

    def test_interrupted_conversion_job_is_recovered(self) -> None:
        original = self.root / "interrupted.key"
        original.write_bytes(b"keynote-placeholder")
        source = candidate_for(original)
        source.extension = ".key"
        derivative = self.root / "interrupted.pptx"
        make_pptx(derivative)
        source_hash, _ = source_sha256(original)
        derivative_hash, _ = source_sha256(derivative)
        database_path = self.root / "interrupted.sqlite3"
        with IndexDatabase(database_path) as database:
            database.initialize()
            work = database.register_candidates([source])
            database.mark_running([work[0]["_job_id"]])
            database.store_result(extract_candidate(work[0]), extractor_version="test")
            pending = database.conversion_candidates()[0]
            database.queue_conversion_import(
                pending,
                derivative,
                method="test-export",
                source_sha256=source_hash,
                derivative_sha256=derivative_hash,
                provenance="verified-export",
            )
            self.assertEqual(database.recover_interrupted_conversion_jobs(), 1)
            self.assertEqual(database.status()["file_status"], {"needs-conversion": 1})
            self.assertEqual(database.status()["job_status"]["queued"], 1)

    def test_v1_migration_is_backed_up_and_idempotent(self) -> None:
        database_path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version', '1');
            CREATE TABLE files(
                id INTEGER PRIMARY KEY, path TEXT UNIQUE, filename TEXT, extension TEXT,
                size_bytes INTEGER, mtime_ns INTEGER, created_time TEXT, modified_time TEXT,
                quick_signature TEXT, is_package INTEGER DEFAULT 0, current_version_id INTEGER,
                status TEXT DEFAULT 'discovered', error TEXT DEFAULT '', source_page TEXT DEFAULT '',
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE versions(
                id INTEGER PRIMARY KEY, file_id INTEGER, content_hash TEXT, extractor_version TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP, derivative_path TEXT DEFAULT '',
                conversion_method TEXT DEFAULT '', title TEXT DEFAULT '', creator TEXT DEFAULT '',
                last_modified_by TEXT DEFAULT '', internal_created TEXT DEFAULT '', internal_modified TEXT DEFAULT '',
                subject TEXT DEFAULT '', source_keywords TEXT DEFAULT '', spotlight_authors TEXT DEFAULT '',
                spotlight_creator TEXT DEFAULT '', spotlight_last_used TEXT DEFAULT '', theme_colors_json TEXT DEFAULT '[]',
                keywords_json TEXT DEFAULT '[]', classification_json TEXT DEFAULT '{}', summary TEXT DEFAULT '',
                needs_visual_review INTEGER DEFAULT 0, visual_reasons_json TEXT DEFAULT '[]', slide_count INTEGER DEFAULT 0,
                text_char_count INTEGER DEFAULT 0, image_count INTEGER DEFAULT 0, UNIQUE(file_id, content_hash)
            );
            CREATE TABLE slides(
                id INTEGER PRIMARY KEY, version_id INTEGER, slide_number INTEGER, title TEXT DEFAULT '', body TEXT DEFAULT '',
                notes TEXT DEFAULT '', word_count INTEGER DEFAULT 0, image_count INTEGER DEFAULT 0,
                table_count INTEGER DEFAULT 0, chart_count INTEGER DEFAULT 0, UNIQUE(version_id, slide_number)
            );
            CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, file_id INTEGER, quick_signature TEXT, stage TEXT DEFAULT 'index',
                status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
                queued_at TEXT DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT,
                UNIQUE(file_id, quick_signature, stage)
            );
            CREATE VIRTUAL TABLE slides_fts USING fts5(title, body, notes, filename, path, keywords, classification);
            """
        )
        connection.close()

        with IndexDatabase(database_path, readonly=True) as database:
            with self.assertRaisesRegex(RuntimeError, "run 'llmwiki-slides migrate'"):
                database.validate_schema()

        class FailingMigrationDatabase(IndexDatabase):
            def _ensure_column(self, table: str, definition: str) -> None:
                if table == "files" and definition.startswith("last_seen_at"):
                    raise RuntimeError("injected migration failure")
                super()._ensure_column(table, definition)

        with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            with FailingMigrationDatabase(database_path) as database:
                database.initialize()
        connection = sqlite3.connect(database_path)
        self.assertEqual(
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0],
            "1",
        )
        self.assertIsNone(
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='scan_runs'").fetchone()
        )
        self.assertNotIn(
            "presence_status",
            {row[1] for row in connection.execute("PRAGMA table_info(files)")},
        )
        connection.close()

        with IndexDatabase(database_path) as database:
            database.initialize()
            backup = database.migration_backup_path
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertEqual(database.integrity_report()["schema_version"], 2)
        with IndexDatabase(database_path) as database:
            database.initialize()
            self.assertIsNone(database.migration_backup_path)
            self.assertEqual(database.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_relation_review_resets_when_version_pair_changes(self) -> None:
        duplicate = self.root / "duplicate.pptx"
        duplicate.write_bytes(self.deck.read_bytes())
        database_path = self.root / "review-reset.sqlite3"
        with IndexDatabase(database_path) as database:
            database.initialize()
            file_ids = []
            for candidate in (candidate_for(self.deck), candidate_for(duplicate)):
                work = database.register_candidates([candidate])
                database.mark_running([work[0]["_job_id"]])
                file_id, _ = database.store_result(extract_candidate(work[0]), extractor_version="test")
                file_ids.append(file_id)
            database.rebuild_relations()
            relation = database.relation_rows()[0]
            database.set_relation_review(relation["id"], "confirmed")
            old_version = database.connection.execute(
                "SELECT current_version_id FROM files WHERE id=?", (file_ids[0],)
            ).fetchone()[0]
            database.connection.execute(
                """
                INSERT INTO versions(file_id, content_hash, extractor_version, source_sha256)
                SELECT file_id, 'synthetic-new-version', extractor_version, source_sha256
                FROM versions WHERE id=?
                """,
                (old_version,),
            )
            new_version = database.connection.execute(
                "SELECT id FROM versions WHERE content_hash='synthetic-new-version'"
            ).fetchone()[0]
            database.connection.execute(
                "UPDATE files SET current_version_id=? WHERE id=?",
                (new_version, file_ids[0]),
            )
            database.connection.commit()
            database.rebuild_relations()
            self.assertEqual(database.relation_rows()[0]["review_status"], "unreviewed")


if __name__ == "__main__":
    unittest.main()
