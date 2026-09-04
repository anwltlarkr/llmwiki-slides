from __future__ import annotations

import tempfile
import unittest
import zipfile
import hashlib
from pathlib import Path

from llmwiki_slides.database import IndexDatabase
from llmwiki_slides.config import AppConfig
from llmwiki_slides.conversion import find_reusable_derivative
from llmwiki_slides.exporter import export_markdown
from llmwiki_slides.extract import extract_candidate, init_worker
from llmwiki_slides.models import Candidate
from llmwiki_slides.vocabulary import classify


SLIDE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Climate model overview</a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Regional simulations improve temperature projections across diverse climates.</a:t></a:r></a:p></p:txBody>
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


def make_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", SLIDE_XML)
        archive.writestr("ppt/theme/theme1.xml", THEME_XML)
        archive.writestr("docProps/core.xml", CORE_XML)


def candidate_for(path: Path) -> Candidate:
    stat = path.stat()
    return Candidate(
        path=str(path),
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
            self.assertEqual(database.register_candidates([candidate]), [])
        text = page.read_text(encoding="utf-8")
        self.assertIn("type: source", text)
        self.assertIn("## Indexed Slide Text", text)
        self.assertIn("Regional simulations improve", text)

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
        source_id = hashlib.sha1(str(original).encode()).hexdigest()[:10]
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
            found = find_reusable_derivative(config, pending)
            self.assertEqual(found, derivative)
            job = database.queue_conversion_import(pending, derivative, method="reused-export")
            self.assertIsNotNone(job)
            indexed = extract_candidate(job)
            self.assertEqual(indexed["extraction_status"], "indexed-converted")
            self.assertEqual(indexed["derivative_path"], str(derivative))
            database.store_result(indexed, extractor_version="test")
            self.assertEqual(database.conversion_candidates(), [])
            self.assertEqual(database.search("temperature projections")[0]["filename"], "legacy.key")


if __name__ == "__main__":
    unittest.main()
