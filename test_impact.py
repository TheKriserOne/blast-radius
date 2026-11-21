import gzip
import json
import unittest
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

from impact import Impact


class ImpactTest(unittest.TestCase):
    def select(self, before, after):
        path = "sqlglot/example.py"
        tests = [{"id": "dependent", "number": 4}, {"id": "unrelated", "number": 8}]
        coverage = {
            "collection": [],
            "tests": {"dependent": [[path, 2]], "unrelated": []},
        }
        diff = f"diff --git a/{path} b/{path}\n" + "".join(
            unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            )
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / path
            source.parent.mkdir()
            source.write_text(before, encoding="utf-8")
            coverage_file = root / "coverage.json.gz"
            coverage_file.write_bytes(gzip.compress(json.dumps(coverage).encode("utf-8")))
            return Impact(tests, coverage_file, root).select(diff)

    def test_encoding_comment_can_break_import(self):
        before = "# π\ndef transform(value):\n    return value\n"
        self.assertIsNone(self.select(before, "# coding: ascii\n" + before))

    def test_selects_callers_of_changed_lambda(self):
        before = "class Factory:\n    callback = lambda value: value + 1\n"
        self.assertEqual(self.select(before, before.replace("+ 1", "+ 2")), {4})

    def test_class_scope_change_falls_back(self):
        before = "class Factory:\n    value = 1\n"
        self.assertIsNone(self.select(before, before.replace("= 1", "= 2")))
