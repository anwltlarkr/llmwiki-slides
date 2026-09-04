from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from llmwiki_slides.config import AppConfig
from llmwiki_slides.scanner import discover


class ScannerTests(unittest.TestCase):
    def test_scans_multiple_formats_and_excludes_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.pptx").write_bytes(b"pptx")
            (root / "two.key").write_bytes(b"key")
            (root / "~$lock.pptx").write_bytes(b"lock")
            ignored = root / "ignored"
            ignored.mkdir()
            (ignored / "three.pptx").write_bytes(b"pptx")
            config = AppConfig(
                vault=root,
                database=root / "index.sqlite3",
                markdown_output=root / "markdown",
                scan_roots=[root],
                exclude_fragments=(str(ignored),),
            )
            found = discover(config)
            self.assertEqual([item.filename for item in found], ["one.pptx", "two.key"])


if __name__ == "__main__":
    unittest.main()
