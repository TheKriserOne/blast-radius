import gzip
import json
import unittest
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

from impact import Impact


class ImpactTest(unittest.TestCase):
    def select(self, before, after, imports=None, collection=()):
        path = "sqlglot/parsers/example.py"
        tests = [
            {"id": "dependent", "number": 4, "file": "tests/test_dependent.py"},
            {"id": "unrelated", "number": 8, "file": "tests/test_unrelated.py"},
        ]
        coverage = {
            "collection": collection,
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
            source.parent.mkdir(parents=True)
            source.write_text(before, encoding="utf-8")
            coverage_file = root / "coverage.json.gz"
            coverage_file.write_bytes(gzip.compress(json.dumps(coverage).encode("utf-8")))
            imports_file = None
            if imports is not None:
                imports_file = root / "import_map.json.gz"
                imports_file.write_bytes(gzip.compress(json.dumps(imports).encode("utf-8")))
            return Impact(tests, coverage_file, root, imports_file).select(diff)

    def test_encoding_comment_can_break_import(self):
        before = "# π\ndef transform(value):\n    return value\n"
        self.assertIsNone(self.select(before, "# coding: ascii\n" + before))

    def test_selects_callers_of_changed_lambda(self):
        before = "class Factory:\n    callback = lambda value: value + 1\n"
        self.assertEqual(self.select(before, before.replace("+ 1", "+ 2")), {4})

    def test_class_scope_change_falls_back(self):
        before = "class Factory:\n    value = 1\n"
        self.assertIsNone(self.select(before, before.replace("= 1", "= 2")))

    def test_parser_table_factory_selects_module_consumers(self):
        path = "sqlglot/parsers/example.py"
        before = (
            "from parser_helpers import factory\n"
            "class Parser:\n"
            "    table = {'key': factory(False)}\n"
            "    keys = tuple(key for key in table)\n"
        )
        imports = {
            "complete_files": ["tests/test_dependent.py", "tests/test_unrelated.py"],
            "files_by_path": {path: ["tests/test_dependent.py"]},
            "factories": {path: {"Parser": ["factory"]}},
        }
        after = before.replace("False", "True")
        self.assertEqual(self.select(before, after, imports), {4})
        self.assertIsNone(self.select(before, after, imports, [(path, 1)]))
        self.assertIsNone(self.select(before, before.replace("False", "0"), imports))
        self.assertIsNone(self.select(before, after + "mutate_shared_state()\n", imports))
        eager_read = before.replace("tuple(key for key in table)", "tuple(table.values())")
        self.assertIsNone(self.select(eager_read, eager_read.replace("False", "True"), imports))

        imports["complete_files"].remove("tests/test_unrelated.py")
        self.assertEqual(self.select(before, after, imports), {4, 8})
