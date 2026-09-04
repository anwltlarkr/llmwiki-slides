from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from llmwiki_slides.config import AppConfig
from llmwiki_slides.database import IndexDatabase
from llmwiki_slides.pipeline import run_update


SLIDE = """<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="1" name="Title"/>
 <p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
 <p:txBody><a:p><a:r><a:t>Pipeline integration</a:t></a:r></a:p></p:txBody>
 </p:sp></p:spTree></p:cSld></p:sld>"""


def make_deck(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", SLIDE)


class PipelineTests(unittest.TestCase):
    def test_update_records_scan_presence_fingerprints_relations_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.pptx"
            second = root / "second.pptx"
            make_deck(first)
            shutil.copyfile(first, second)
            config = AppConfig(
                vault=root,
                database=root / "meta/index.sqlite3",
                markdown_output=root / "wiki/presentations",
                scan_roots=[root],
                extract_workers=2,
                scan_workers=1,
                start_method="spawn",
            )

            summary = run_update(config, update_qmd=False)
            self.assertEqual(summary["scan_status"], "complete")
            self.assertEqual(summary["processed"], 2)
            self.assertEqual(summary["presence_status"], {"present": 2})
            self.assertEqual(summary["relations"], {"exact-duplicate": 1})
            self.assertEqual(summary["exported"], 2)
            pages = sorted(config.markdown_output.glob("*.md"))
            self.assertEqual(len(pages), 2)
            self.assertIn("## Integrity Fingerprints", pages[0].read_text(encoding="utf-8"))

            repeated = run_update(config, update_qmd=False)
            self.assertEqual(repeated["processed"], 0)
            self.assertEqual(repeated["presence_reconciliation"]["marked_missing"], 0)

            first.write_bytes(first.read_bytes() + b"changed")
            limited = run_update(config, limit=0, update_qmd=False)
            self.assertEqual(limited["processed"], 0)
            self.assertEqual(limited["queued"], 1)
            with IndexDatabase(config.database, readonly=True) as database:
                database.validate_schema()
                row = database.connection.execute(
                    "SELECT status, source_sha256 FROM files WHERE filename='first.pptx'"
                ).fetchone()
                self.assertEqual(row["status"], "indexed")
                self.assertEqual(row["source_sha256"], "")

    def test_mixed_partial_scan_does_not_reconcile_any_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deck = root / "present.pptx"
            make_deck(deck)
            config = AppConfig(
                vault=root,
                database=root / "meta/index.sqlite3",
                markdown_output=root / "wiki/presentations",
                scan_roots=[root],
                extract_workers=1,
                scan_workers=1,
                start_method="spawn",
            )
            run_update(config, update_qmd=False)
            deck.unlink()
            config.scan_roots = [root, root / "missing-root"]
            partial = run_update(config, update_qmd=False)
            self.assertEqual(partial["scan_status"], "partial")
            self.assertEqual(partial["presence_reconciliation"]["marked_missing"], 0)
            with IndexDatabase(config.database, readonly=True) as database:
                database.validate_schema()
                self.assertEqual(database.status()["presence_status"], {"present": 1})


if __name__ == "__main__":
    unittest.main()
