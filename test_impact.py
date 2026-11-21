import gzip
import json
import unittest
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

from factory_probe import opaque as factory_opaque, tag as factory_tag
from identity_comparisons import pure_equivalent
from impact import Impact


class ImpactTest(unittest.TestCase):
    def select(
        self, before, after, imports=None, collection=(), *,
        path="sqlglot/parsers/example.py", expression_validators=None, dialect_reads=None,
        identity_reads=None, factory_calls=None, guard_reads=None,
    ):
        tests = [
            {"id": "dependent", "number": 4, "file": "tests/test_dependent.py"},
            {"id": "unrelated", "number": 8, "file": "tests/test_unrelated.py"},
        ]
        coverage = {
            "collection": collection,
            "tests": {"dependent": [[path, 2]], "unrelated": []},
        }
        if expression_validators is not None:
            coverage["expression_validation"] = True
            for test_id, dependencies in expression_validators.items():
                coverage["tests"][test_id].extend(dependencies)
        if dialect_reads is not None:
            coverage["dialect_fields"] = True
            for test_id, dependencies in dialect_reads.items():
                coverage["tests"][test_id].extend(dependencies)
        if identity_reads is not None:
            coverage["identity_comparisons"] = True
            for test_id, dependencies in identity_reads.items():
                coverage["tests"][test_id].extend(dependencies)
        if factory_calls is not None:
            coverage["factory_calls"] = True
            for test_id, dependencies in factory_calls.items():
                coverage["tests"][test_id].extend(dependencies)
        if guard_reads is not None:
            coverage["guard_coverage"] = True
            for test_id, dependencies in guard_reads.items():
                coverage["tests"][test_id].extend(dependencies)
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

    def test_show_parser_table_factory_can_select_exact_calls(self):
        path = "sqlglot/parsers/example.py"
        before = "from parser_helpers import factory\nclass Parser:\n    SHOW_PARSERS = {'key': factory(False)}\n"
        after = before.replace("False", "True")
        imports = {
            "complete_files": ["tests/test_dependent.py", "tests/test_unrelated.py"],
            "files_by_path": {path: ["tests/test_dependent.py", "tests/test_unrelated.py"]},
            "factories": {path: {"Parser": ["factory"]}},
        }
        site = ("Parser", "SHOW_PARSERS", "key")
        registered = [factory_tag("registered", path, site)]
        called = factory_tag("called", path, site)
        self.assertEqual(self.select(
            before, after, imports, factory_calls={"dependent": registered},
        ), set())
        self.assertEqual(self.select(
            before, after, imports, factory_calls={"dependent": [*registered, called]},
        ), {4})
        self.assertEqual(self.select(
            before, after, imports,
            factory_calls={"dependent": registered, "unrelated": [factory_opaque(path)]},
        ), {4, 8})
        self.assertEqual(self.select(
            before, after, imports, factory_calls={},
        ), {4, 8})

    def test_expression_flag_includes_classes_copying_argument_types(self):
        path = "sqlglot/expressions/example.py"
        grant = ("@expr:sqlglot.expressions.example.Grant", 0)
        revoke = ("@expr:sqlglot.expressions.example.Revoke", 0)
        before = (
            "class Grant(Expression):\n"
            "    arg_types = {'kind': False}\n"
            "class Revoke(Expression):\n"
            "    arg_types = {**Grant.arg_types, 'cascade': True}\n"
        )
        after = before.replace("False", "True")
        profiling = {"dependent": [grant], "unrelated": [revoke]}
        self.assertIsNone(self.select(before, after, path=path))
        self.assertEqual(self.select(
            before, after, path=path, expression_validators=profiling,
        ), {4, 8})
        self.assertIsNone(self.select(
            before, after, path=path, expression_validators={"dependent": [grant]},
        ))
        self.assertIsNone(self.select(
            before, after, path=path, collection=[revoke], expression_validators=profiling,
        ))
        self.assertIsNone(self.select(
            before, before.replace("'kind'", "'role'"),
            path=path, expression_validators=profiling,
        ))

    def test_week_offset_selects_readers_only_for_literal_values(self):
        path = "sqlglot/dialects/example.py"
        before = "class Example(Dialect):\n    WEEK_OFFSET = -1\n"
        reads = {"dependent": [("@week:sqlglot.dialects.example.Example", 0)]}
        self.assertEqual(self.select(
            before, before.replace("-1", "2"), path=path, dialect_reads=reads,
        ), {4})
        self.assertIsNone(self.select(
            before, before.replace("-1", "int('2')"), path=path, dialect_reads=reads,
        ))

    def test_numeric_escape_selects_exact_key_and_unrecognized_readers(self):
        path = "sqlglot/dialects/example.py"
        before = (
            "class Example(Dialect):\n"
            "    class Tokenizer:\n"
            "        NUMERIC_ESCAPES = {'u': (16, 4, 4, 65535)}\n"
        )
        after = before.replace("(16,", "(17,")
        owner = "sqlglot.dialects.example.Example.Tokenizer"
        reads = {
            "dependent": [(f"@numeric-owner:{owner}", 0), (f"@numeric:{owner}:u", 0)],
            "unrelated": [(f"@numeric:{owner}:x", 0)],
        }
        self.assertEqual(self.select(before, after, path=path, dialect_reads=reads), {4})
        reads["unrelated"] = [("@numeric-unknown:x", 0)]
        self.assertEqual(self.select(before, after, path=path, dialect_reads=reads), {4, 8})
        self.assertIsNone(self.select(
            before, before.replace("(16, 4, 4, 65535)", "0"), path=path, dialect_reads=reads,
        ))
        self.assertIsNone(self.select(
            before, before.replace("'u'", "'x'"), path=path, dialect_reads=reads,
        ))
        reads = {"dependent": [(f"@numeric-owner:{owner}", 0)]}
        self.assertEqual(self.select(before, after, path=path, dialect_reads=reads), set())

    def test_identity_rewrite_keeps_uncertified_tests_and_code_readers(self):
        path = "sqlglot/executor/example.py"
        before = "def check(value):\n    return value is None\n"
        after = before.replace("is None", "== None")
        site = f"{path}:2:11"
        safe = [(f"@identity-site:{site}", 0)]
        unsafe = (f"@identity-unsafe:{site}", 0)
        self.assertEqual(self.select(
            before, after, collection=safe, path=path, identity_reads={},
        ), set())
        self.assertEqual(self.select(
            before, after, collection=safe, path=path,
            identity_reads={"unrelated": [unsafe]},
        ), {8})
        self.assertEqual(self.select(
            before, after, collection=safe, path=path,
            identity_reads={"unrelated": [(path, 0)]},
        ), {8})
        self.assertEqual(self.select(
            before, after, collection=[*safe, (f"@identity-code:{path}:5", 0)],
            path=path, identity_reads={"unrelated": [(f"@identity-code:{path}:1", 0)]},
        ), {8})
        self.assertIsNone(self.select(
            before, after, collection=[*safe, unsafe], path=path, identity_reads={},
        ))
        self.assertEqual(self.select(
            before, after, path=path, identity_reads={},
        ), {4})

    def test_identity_probe_never_invokes_custom_comparators(self):
        calls = []

        class Value:
            __module__ = "sqlglot.executor.example"

            def __eq__(self, other):
                calls.append(other)
                return False

        class Meta(type):
            def __eq__(self, other):
                calls.append(other)
                return False

        class Text(str, metaclass=Meta):
            __module__ = "sqlglot.dialects.example"

        text = Text("same")
        self.assertFalse(pure_equivalent(Value(), None, False))
        self.assertFalse(pure_equivalent(text, Text("same"), False))
        self.assertTrue(pure_equivalent(text, text, True))
        self.assertEqual(calls, [])

    def test_boolean_guard_selects_tests_that_reach_the_condition(self):
        path = "sqlglot/parsers/example.py"
        before = "def check(rest, current):\n    if rest and current:\n        return current\n"
        after = before.replace("and", "or")
        site = f"{path}:2:7"
        registration = (f"@guard-site:{site}", 0)
        read = (f"@guard-read:{site}", 0)
        reads = {"unrelated": [read]}
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, guard_reads=reads,
        ), {8})
        self.assertEqual(self.select(
            before, after, path=path, guard_reads=reads,
        ), {4})
        self.assertEqual(self.select(
            before, after.replace("or current", "or not current"),
            collection=[registration], path=path, guard_reads=reads,
        ), {4})
        self.assertIsNone(self.select(
            before, after, collection=[registration, read], path=path, guard_reads=reads,
        ))
