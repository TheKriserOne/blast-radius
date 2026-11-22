import gzip
import json
import unittest
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from build_syntax import build_syntax
from dialect_fields import TYPE_MEMBERS_SOURCE
from expression_arguments import possible_argument_classes
from expression_probe import missing_optional_arguments
from factory_changes import FUNCTION_BUILDERS_SOURCE
from factory_probe import FUNCTION_TABLES, MATCHED_TABLE_SITES
from factory_probe import opaque as factory_opaque, tag as factory_tag
from fixture_reads import filtered_dependency, pair_dependency
from identity_comparisons import pure_equivalent
from impact import Impact, apply_hunks
from module_bindings import AUDITED_BINDINGS, binding_read_lines
from ordered_comparisons import observe_order


class ImpactTest(unittest.TestCase):
    def select(
        self, before, after, imports=None, collection=(), *,
        path="sqlglot/parsers/example.py", expression_validators=None, dialect_reads=None,
        identity_reads=None, factory_calls=None, guard_reads=None, literal_reads=None, fixture_reads=None,
        cached_source=None, ordered_reads=None, argument_presence=False, extra_changes=(), type_members=(),
        function_builders=(), explicit_source_reads=False,
    ):
        tests = [
            {"id": "dependent", "number": 4, "file": "tests/test_dependent.py"},
            {"id": "unrelated", "number": 8, "file": "tests/test_unrelated.py"},
        ]
        coverage = {
            "collection": collection,
            "tests": {"dependent": [[path, 2]], "unrelated": []},
            "explicit_source_reads": explicit_source_reads,
        }
        if expression_validators is not None:
            coverage["expression_validation"] = True
            coverage["argument_presence"] = argument_presence
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
        if literal_reads is not None:
            coverage["literal_coverage"] = True
            for test_id, dependencies in literal_reads.items():
                coverage["tests"][test_id].extend(dependencies)
        for reads in (fixture_reads, ordered_reads):
            if reads is not None:
                for test_id, dependencies in reads.items():
                    coverage["tests"][test_id].extend(dependencies)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            diff = []
            for file, original, updated in ((path, before, after), *extra_changes):
                source = root / file
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(original, encoding="utf-8")
                diff.append(f"diff --git a/{file} b/{file}\n")
                diff.extend(unified_diff(
                    original.splitlines(keepends=True), updated.splitlines(keepends=True),
                    fromfile=f"a/{file}", tofile=f"b/{file}",
                ))
            if type_members:
                dtype_source = root / TYPE_MEMBERS_SOURCE
                dtype_source.parent.mkdir(parents=True, exist_ok=True)
                dtype_source.write_text("# test type member registry\n", encoding="utf-8")
            if function_builders:
                builder_source = root / FUNCTION_BUILDERS_SOURCE
                builder_source.parent.mkdir(parents=True, exist_ok=True)
                builder_source.write_text("# test function builder registry\n", encoding="utf-8")
            if cached_source is not None or type_members or function_builders:
                source = root / path
                if cached_source is not None:
                    source.write_text(cached_source, encoding="utf-8")
                build_syntax(root, type_members, function_builders)
                source.write_text(before, encoding="utf-8")
            coverage_file = root / "coverage.json.gz"
            coverage_file.write_bytes(gzip.compress(json.dumps(coverage).encode("utf-8")))
            imports_file = None
            if imports is not None:
                imports_file = root / "import_map.json.gz"
                imports_file.write_bytes(gzip.compress(json.dumps(imports).encode("utf-8")))
            return Impact(tests, coverage_file, root, imports_file).select("".join(diff))

    def test_encoding_comment_can_break_import(self):
        before = "# π\ndef transform(value):\n    return value\n"
        self.assertIsNone(self.select(before, "# coding: ascii\n" + before))

    def test_cached_syntax_still_checks_source_and_patch(self):
        before = "def check():\n    return True\n"
        after = before.replace("True", "False")
        self.assertEqual(self.select(before, after, cached_source=after), {4})
        invalid = before.replace("    return", "    break\n    return")
        self.assertIsNone(self.select(before, invalid, cached_source=before))

    def test_cached_matcher_uses_only_the_matching_baseline(self):
        path = "sqlglot/parsers/example.py"
        before = "def check(left, right):\n    if left and right:\n        return True\n"
        after = before.replace("and", "or")
        site = f"{path}:2:7"
        options = {
            "path": path,
            "collection": [(f"@guard-site:{site}", 0)],
            "guard_reads": {"unrelated": [(f"@guard-read:{site}", 0)]},
        }
        with patch("syntax_profile.baseline_profile", side_effect=AssertionError("reparsed cached baseline")):
            self.assertEqual(self.select(before, after, cached_source=before, **options), {8})
        self.assertEqual(self.select(before, after, cached_source=after, **options), {8})

    def test_dependency_aliases_keep_collection_and_catalog_subsets(self):
        dependency, alias = ("sqlglot/example.py", 1), ("sqlglot/example.py", 2)
        absent, absent_alias = ("sqlglot/other.py", 1), ("sqlglot/other.py", 2)
        coverage = {
            "dependencies": [dependency, alias, absent, absent_alias],
            "collection": [0],
            "tests": {"included": [0], "excluded": [2]},
            "aliases": [[1, 0], [3, 2]],
        }
        tests = [{"id": "included", "number": 4}, {"id": "unprofiled", "number": 8}]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            coverage_file = root / "coverage.json.gz"
            coverage_file.write_bytes(gzip.compress(json.dumps(coverage).encode("utf-8")))
            impact = Impact(tests, coverage_file, root)
            self.assertEqual(impact.dependents, {dependency: {4}, alias: {4}})
            self.assertIs(impact.dependents[alias], impact.dependents[dependency])
            self.assertEqual(impact.collection, {dependency})
            self.assertEqual(impact.uncovered, {8})

    def test_hunks_preserve_non_lf_separators_and_unterminated_rows(self):
        hunks = (
            "@@ -1,2 +1,2 @@\n before\rafter\n"
            "-last\n\\ No newline at end of file\n"
            "+new\u2028last\n\\ No newline at end of file\n"
        )
        self.assertEqual(apply_hunks("before\rafter\nlast", hunks), "before\rafter\nnew\u2028last")

    def test_hunks_require_unterminated_rows_to_be_last(self):
        with self.assertRaises(ValueError):
            apply_hunks("one\n", "@@ -1 +1,2 @@\n-one\n+two\n\\ No newline at end of file\n+three\n")
        with self.assertRaises(ValueError):
            apply_hunks("one", "@@ -1,0 +2 @@\n+two\n")

    def test_selects_callers_of_changed_lambda(self):
        before = "class Factory:\n    callback = lambda value: value + 1\n"
        self.assertEqual(self.select(before, before.replace("+ 1", "+ 2")), {4})

    def test_class_scope_change_falls_back(self):
        before = "class Factory:\n    value = 1\n"
        self.assertIsNone(self.select(before, before.replace("= 1", "= 2")))

    def test_audited_module_binding_keeps_runtime_and_raw_readers(self):
        path = "sqlglot/tokenizer_core.py"
        before = "_KEYS: frozenset[str] = frozenset('12')\ndef read(key):\n    return key in _KEYS\n"
        after = before.replace("'12'", "'13'")
        digest = sha256(before.encode("utf-8")).hexdigest()
        with patch.dict(AUDITED_BINDINGS, {path: (digest, "_KEYS", "frozenset")}):
            self.assertEqual(self.select(before, after, path=path), {4})
            self.assertEqual(
                self.select(before, after, path=path, fixture_reads={"unrelated": [(path, 0)]}),
                {4, 8},
            )
            self.assertIsNone(self.select(before, after, path=path, collection=[(path, 2)]))
            self.assertEqual(self.select(before, after, path=path, collection=[(f"@identity-code:{path}:2", 0)]), {4})
            self.assertIsNone(self.select(before, after, path=path, collection=[(f"@identity-code:{path}:1", 0)]))
            self.assertIsNone(self.select(before, after.replace("def read", "\ndef read"), path=path))
            self.assertIsNone(self.select(before, after.replace("= frozenset", "= set"), path=path))
            self.assertIsNone(self.select(before + "# different base\n", after, path=path))
            self.assertIsNone(binding_read_lines(path, before, after.replace("'13'", r"'\q'")))

    def test_fixture_comment_preserves_filtered_reads(self):
        path = "tests/fixtures/example.sql"
        before = "SELECT 1;\nSELECT 1;\n"
        after = before + "-- comment\n"
        filtered = filtered_dependency(path)
        self.assertEqual(self.select(before, after, path=path, fixture_reads={
            "dependent": [filtered],
        }), set())
        reads = {"dependent": [filtered], "unrelated": [(path, 0)]}
        self.assertEqual(self.select(before, after, path=path, fixture_reads=reads), {8})
        self.assertEqual(self.select(before, after.replace("--", "  --"), path=path, fixture_reads=reads), {4, 8})
        self.assertIsNone(self.select(before, after, collection=[(path, 0)], path=path, fixture_reads=reads))
        self.assertEqual(self.select(before, after, collection=[filtered], path=path, fixture_reads=reads), {8})

    def test_fixture_pair_equivalence_keeps_line_and_raw_readers(self):
        path = "tests/fixtures/example.sql"
        before = "# dialect: sqlite\nSELECT 1;\nSELECT 1;\n"
        after = "### dialect : sqlite  \nSELECT 1  ;\n  SELECT 1 \t;\n"
        pairs = pair_dependency(path)
        self.assertEqual(self.select(before, after, path=path, fixture_reads={
            "dependent": [pairs],
        }), set())
        for dependency in (filtered_dependency(path), (path, 0)):
            self.assertEqual(self.select(before, after, path=path, fixture_reads={
                "dependent": [pairs], "unrelated": [dependency],
            }), {8})

    def test_fixture_pairs_preserve_metadata_order_and_errors(self):
        path = "tests/fixtures/example.sql"
        before = "# dialect: sqlite\n# identify: true\nSELECT 1;\nSELECT 1;\n"
        reordered = "# identify: true\n# dialect: sqlite\nSELECT 1;\nSELECT 1;\n"
        malformed = before.replace("# dialect:", "# dialect")
        metadata_only = "# dialect: sqlite;\nSELECT 1;\n"
        interior_space = before.replace("SELECT 1", "SELECT  1")
        reads = {"dependent": [pair_dependency(path)]}
        for after in (reordered, malformed, metadata_only, interior_space):
            self.assertEqual(self.select(before, after, path=path, fixture_reads=reads), {4})

    def test_equivalent_fixture_allows_single_source_selection(self):
        path = "sqlglot/parsers/example.py"
        before = "def check(value):\n    if value:\n        return value\n"
        after = before.replace("if value:", "if not value:")
        fixture = "tests/fixtures/example.sql"
        change = (fixture, "SELECT 1;\nSELECT 1;\n", "SELECT 1 ;\nSELECT 1;\n")
        parameters = {
            "path": path,
            "collection": [(f"@guard-site:{path}:2:7", 0)],
            "guard_reads": {"unrelated": [(f"@guard-read:{path}:2:7", 0)]},
            "extra_changes": [change],
        }
        self.assertEqual(self.select(before, after, fixture_reads={
            "dependent": [pair_dependency(fixture)],
        }, **parameters), {8})
        self.assertEqual(self.select(before, after, fixture_reads={
            "dependent": [pair_dependency(fixture), (fixture, 0)],
        }, **parameters), {4})

    def test_equivalent_source_allows_single_source_selection(self):
        path = "sqlglot/parsers/example.py"
        before = "def check(value):\n    if value:\n        return value\n"
        after = before.replace("if value:", "if not value:")
        helper = "sqlglot/helper.py"
        source = "'''Café'''\nsetting = 1  # original\n"
        parameters = {
            "path": path,
            "collection": [(f"@guard-site:{path}:2:7", 0)],
            "guard_reads": {"unrelated": [(f"@guard-read:{path}:2:7", 0)]},
        }

        def select(updated, explicit_source_reads=True, **options):
            return self.select(before, after, **(parameters | options),
                               extra_changes=[(helper, source, updated)], explicit_source_reads=explicit_source_reads)

        updated = source.replace("# original", "# revised")
        self.assertEqual(select(updated), {8})
        self.assertEqual(select(source + "# tail\n"), {8})
        self.assertEqual(select(updated, explicit_source_reads=False), {4})
        self.assertEqual(select(updated, fixture_reads={"dependent": [(helper, 0)]}), {4})
        self.assertIsNone(select(updated, collection=[(helper, 0)]))
        for changes_behavior in (
            "# leading\n" + source,
            "# coding: ascii\n" + source,
            updated.replace("Café", "Tea"),
            source.replace("# original", "# type: ignore[assignment]"),
        ):
            self.assertIn(select(changes_behavior), ({4}, None))

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

    def test_new_required_argument_selects_missing_and_opaque_validators(self):
        path = "sqlglot/expressions/example.py"
        before = "class Options(Expression):\n    arg_types = {'flag': False}\n"
        after = before.replace("False", "True")
        name = "sqlglot.expressions.example.Options"
        validator = (f"@expr:{name}", 0)
        options = {"path": path, "argument_presence": True}
        for dependency in (f"@expr-missing:{name}:flag", f"@expr-opaque:{name}"):
            self.assertEqual(self.select(before, after, expression_validators={
                "dependent": [validator], "unrelated": [validator, (dependency, 0)],
            }, **options), {8})
        self.assertEqual(self.select(before, after, expression_validators={
            "dependent": [validator], "unrelated": [validator, (path, 0)],
        }, **options), {8})
        self.assertEqual(self.select(after, before, expression_validators={
            "dependent": [validator],
        }, **options), {4})

    def test_argument_coverage_keeps_classes_copying_flags(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sqlglot/expressions/example.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "class Grant(Expression):\n    arg_types = {'kind': False}\n"
                "class Revoke(Expression):\n    arg_types = {**Grant.arg_types, 'cascade': True}\n"
                "class Unrelated(Expression):\n    arg_types = external()\n",
                encoding="utf-8",
            )
            self.assertEqual(possible_argument_classes(root), {
                "sqlglot.expressions.example.Grant", "sqlglot.expressions.example.Revoke",
            })

    def test_argument_presence_probe_keeps_custom_values_and_multiple_errors(self):
        class Options:
            __slots__ = ("args", "__dict__")
            arg_types = {"first": True, "second": True, "flag": False}
            required_args = {"first", "second"}

        class CustomValue:
            @property
            def __class__(self):
                raise AssertionError("probe called a custom getter")

        class CustomOptions(Options):
            @property
            def args(self):
                raise AssertionError("probe called a custom argument getter")

        class CustomClass(Options):
            @property
            def __class__(self):
                raise AssertionError("probe called a custom class getter")

        class HiddenFields(Options):
            @property
            def __dict__(self):
                raise AssertionError("probe called a custom fields getter")

        slot = Options.__dict__["args"]
        dict_slot = Options.__dict__["__dict__"]
        expression = Options()
        expression.args = {"first": 1, "second": 2, "flag": False}
        self.assertEqual(missing_optional_arguments(expression, slot, dict_slot), set())
        expression.args["flag"] = CustomValue()
        self.assertEqual(missing_optional_arguments(expression, slot, dict_slot), {"flag"})
        expression.args = {"flag": False}
        self.assertIsNone(missing_optional_arguments(expression, slot, dict_slot))
        present = {"first": 1, "second": 2, "flag": False}
        for subtype in (CustomOptions, CustomClass, HiddenFields):
            custom = subtype()
            slot.__set__(custom, present)
            self.assertIsNone(missing_optional_arguments(custom, slot, dict_slot))
        expression.args = present
        expression.required_args = {"first"}
        self.assertIsNone(missing_optional_arguments(expression, slot, dict_slot))

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

    def test_numeric_escape_key_rename_selects_hits_and_misses(self):
        path = "sqlglot/dialects/example.py"
        before = (
            "class Example:\n"
            "    class Tokenizer:\n"
            "        NUMERIC_ESCAPES = {'U': (16, 8, 8, 0x10FFFF)}\n"
        )
        after = before.replace("'U'", "'V'")
        owner = "sqlglot.dialects.example.Example.Tokenizer"
        hit = (f"@numeric:{owner}:U", 0)
        miss = (f"@numeric-miss:{owner}:V", 0)
        capability = ("@numeric-missing-keys", 0)
        reads = {"dependent": [(f"@numeric-owner:{owner}", 0), hit], "unrelated": [miss]}
        self.assertIsNone(self.select(before, after, path=path, dialect_reads=reads))
        options = {"path": path, "collection": [capability], "dialect_reads": reads}
        self.assertEqual(self.select(before, after, **options), {4, 8})
        self.assertIsNone(self.select(before, after.replace("'V'", "'x'"), **options))
        self.assertIsNone(self.select(before, before.replace("{'U': (16, 8, 8, 0x10FFFF)}", "{}"), **options))
        self.assertIsNone(self.select(before, after, path=path, collection=[capability, miss], dialect_reads=reads))
        reads["unrelated"] = [("@numeric-unknown-miss:V", 0)]
        self.assertEqual(self.select(before, after, **options), {4, 8})
        reads["unrelated"] = [("@numeric-unknown-miss:W", 0)]
        self.assertEqual(self.select(before, after, **options), {4})

    def test_type_mapping_value_uses_exact_key_and_keeps_other_readers(self):
        path = "sqlglot/generators/example.py"
        before = (
            "class Example:\n"
            "    TYPE_MAPPING: dict = {**Base.TYPE_MAPPING, exp.DType.TEXT: 'STRING', exp.DType.BLOB: 'BINARY'}\n"
        )
        after = before.replace("'STRING'", "'VARCHAR'")
        owner = "sqlglot.generators.example.Example"
        lookup = (f"@type-map:{owner}:TEXT", 0)
        other = (f"@type-map:{owner}:BLOB", 0)
        coverage = [("@type-map-sites:sqlglot.generator", 0), ("@type-map-sites:sqlglot.expressions.builders", 0)]
        reads = {"dependent": [(f"@type-map-owner:{owner}", 0), other], "unrelated": [lookup]}
        self.assertIsNone(self.select(before, after, path=path, collection=coverage[:1], dialect_reads=reads))
        options = {"path": path, "collection": coverage, "dialect_reads": reads}
        self.assertEqual(self.select(before, after, **options), {8})
        self.assertIsNone(self.select(before, before.replace("exp.DType.TEXT", "exp.DType.INT"), **options))
        self.assertIsNone(self.select(before, after, path=path, collection=[*coverage, lookup], dialect_reads=reads))
        for reader in (("@type-map-unknown", 0), (f"@identity-code:{path}:1", 0), (path, 0)):
            reads["dependent"].append(reader)
            self.assertEqual(self.select(before, after, **options), {4, 8})
            reads["dependent"].pop()

    def test_type_mapping_key_edits_use_hits_and_misses_for_valid_members(self):
        path = "sqlglot/generators/example.py"
        before = (
            "class Example:\n"
            "    TYPE_MAPPING = {exp.DType.TEXT: 'STRING', **Base.TYPE_MAPPING, exp.DType.BLOB: 'BINARY'}\n"
        )
        owner = "sqlglot.generators.example.Example"
        reads = {
            "dependent": [(f"@type-map-owner:{owner}", 0), (f"@type-map:{owner}:TEXT", 0)],
            "unrelated": [(f"@type-map:{owner}:DATE", 0)],
        }
        registered = [("@type-map-sites:sqlglot.generator", 0), ("@type-map-sites:sqlglot.expressions.builders", 0)]
        options = {"path": path, "collection": registered, "dialect_reads": reads}
        members = ("TEXT", "BLOB", "DATE", "TIMETZ")
        rename = before.replace("exp.DType.TEXT", "exp.DType.DATE")
        self.assertIsNone(self.select(before, rename, **options))
        self.assertEqual(self.select(before, rename, type_members=members, **options), {4, 8})
        self.assertIsNone(self.select(before, rename.replace("DType.DATE", "DType.INVALID"), type_members=members, **options))
        self.assertEqual(self.select(before, before.replace("exp.DType.TEXT: 'STRING', ", ""), **options), {4})
        moved = before.replace("exp.DType.TEXT: 'STRING', **Base.TYPE_MAPPING", "**Base.TYPE_MAPPING, exp.DType.TEXT: 'STRING'")
        self.assertEqual(self.select(before, moved, **options), {4})
        added = before.replace("**Base.TYPE_MAPPING,", "**Base.TYPE_MAPPING, exp.DType.TIMETZ: 'TIME',")
        self.assertEqual(self.select(before, added, type_members=members, **options), set())

    def test_function_keys_keep_eager_bindings_and_trace_the_renamed_keys(self):
        path = "sqlglot/parsers/example.py"
        before = "class Example:\n    FUNCTIONS = {**Base.FUNCTIONS, 'OLD': make_handler(), 'KEEP': lambda x: x}\n"
        owner = "sqlglot.parsers.example.Example"
        old_lookup = (f"@parser-functions:{owner}:OLD", 0)
        new_lookup = (f"@parser-functions:{owner}:NEW", 0)
        sites = [(f"@parser-functions-sites:{module}", 0) for module in sorted(FUNCTION_TABLES["FUNCTIONS"][1])]
        reads = {"dependent": [(f"@parser-functions-owner:{owner}", 0), old_lookup], "unrelated": [new_lookup]}
        after = before.replace("'OLD'", "'NEW'")
        self.assertIsNone(self.select(before, after, path=path, collection=sites[:1], dialect_reads=reads))
        self.assertEqual(self.select(before, after, path=path, collection=sites, dialect_reads=reads), {4, 8})
        self.assertIsNone(self.select(before, after, path=path, collection=[*sites, new_lookup], dialect_reads=reads))
        removed = before.replace("'OLD': make_handler(), ", "")
        self.assertIsNone(self.select(before, removed, path=path, collection=sites, dialect_reads=reads))

    def test_function_keys_accept_pure_lambda_and_certified_constructor_changes(self):
        path = "sqlglot/parsers/example.py"
        before = "from sqlglot import exp\nclass Example:\n    FUNCTIONS = {'KEEP': lambda args: args[0]}\n"
        owner = "sqlglot.parsers.example.Example"
        sites = [(f"@parser-functions-sites:{module}", 0) for module in sorted(FUNCTION_TABLES["FUNCTIONS"][1])]
        reads = {
            "dependent": [(f"@parser-functions-owner:{owner}", 0), (f"@parser-functions:{owner}:KEEP", 0)],
            "unrelated": [(f"@parser-functions:{owner}:NEW", 0)],
        }
        options = {"path": path, "collection": sites, "dialect_reads": reads}
        self.assertEqual(self.select(before, before.replace("'KEEP': lambda args: args[0]", ""), **options), {4})
        added = before.replace("{", "{'NEW': exp.CurrentTime.from_arg_list, ")
        self.assertIsNone(self.select(before, added, **options))
        self.assertEqual(self.select(before, added, function_builders=("CurrentTime",), **options), {8})
        self.assertIsNone(self.select(before, added.replace("CurrentTime", "Missing"), function_builders=("CurrentTime",), **options))
        reads["dependent"].append(("@parser-functions-unknown:OTHER", 0))
        self.assertEqual(self.select(before, added, function_builders=("CurrentTime",), **options), {4, 8})

    def test_function_key_edits_keep_only_changed_compiled_code_readers(self):
        path = "sqlglot/parsers/example.py"
        before = (
            "def unrelated():\n"
            "    return True\n"
            "class Example:\n"
            "    FUNCTIONS = {'OLD': make_handler(), 'CLONE': lambda args: args[0]}\n"
            "    def get(self):\n"
            "        return 1\n"
        )
        owner = "sqlglot.parsers.example.Example"
        unchanged_reader = (f"@identity-code:{path}:1", 0)
        later_reader = (f"@identity-code:{path}:5", 0)
        sites = [(f"@parser-functions-sites:{module}", 0) for module in sorted(FUNCTION_TABLES["FUNCTIONS"][1])]
        reads = {"dependent": [(f"@parser-functions-owner:{owner}", 0)],
                 "unrelated": [(f"@parser-functions:{owner}:OLD", 0)]}
        rename = before.replace("'OLD'", "'NEW'")
        self.assertEqual(
            self.select(before, rename, path=path, collection=[*sites, unchanged_reader, later_reader], dialect_reads=reads),
            {8},
        )
        shifted = before.replace("    def get", "\n    def get")
        shifted = shifted.replace("make_handler(),", "make_handler(), 'NEW': lambda args: args[0],")
        self.assertIsNone(self.select(before, shifted, path=path, collection=[*sites, later_reader], dialect_reads=reads))

    def test_function_dispatch_has_independent_coverage_and_preserves_import_alias(self):
        path = "sqlglot/parser.py"
        before = (
            "class Parser:\n"
            "    FUNCTIONS = {'SRC': lambda arg: arg}\n"
            "    FUNCTION_PARSERS = {'SRC': lambda self: self.read(), 'TRY_CAST': lambda self: self.cast()}\n"
        )
        owner = "sqlglot.parser.Parser"
        function_tag, function_modules = FUNCTION_TABLES["FUNCTIONS"]
        dispatch_tag, dispatch_modules = FUNCTION_TABLES["FUNCTION_PARSERS"]
        function_sites = [(f"{function_tag}-sites:{module}", 0) for module in function_modules]
        dispatch_sites = [(f"{dispatch_tag}-sites:{module}", 0) for module in dispatch_modules]
        reads = {
            "dependent": [(f"{function_tag}-owner:{owner}", 0), (f"{function_tag}:{owner}:SRC", 0)],
            "unrelated": [(f"{dispatch_tag}-owner:{owner}", 0), (f"{dispatch_tag}:{owner}:SRC", 0)],
        }
        changed = before.replace("'SRC': lambda self", "'DST': lambda self")
        self.assertIsNone(self.select(before, changed, path=path, collection=function_sites, dialect_reads=reads))
        self.assertEqual(self.select(before, changed, path=path, collection=dispatch_sites, dialect_reads=reads), {8})
        combined = changed.replace("'SRC': lambda arg", "'DST': lambda arg")
        options = {"path": path, "collection": [*dispatch_sites, *function_sites], "dialect_reads": reads}
        self.assertEqual(self.select(before, combined, **options), {4, 8})
        self.assertIsNone(self.select(before, before.replace("self.read()", "self.write()"), **options))
        self.assertIsNone(self.select(before, before.replace("'TRY_CAST'", "'TRY_OTHER_CAST'"), **options))

    def test_property_keys_select_existing_and_new_lookups(self):
        path = "sqlglot/parser.py"
        before = (
            "class Parser:\n"
            "    PROPERTY_PARSERS = {\n"
            "        'ENGINE': lambda self: self.parse_engine(),\n"
            "        'HEAP': lambda self: self.parse_heap(),\n"
            "    }\n"
        )
        owner = "sqlglot.parser.Parser"
        tag, modules = FUNCTION_TABLES["PROPERTY_PARSERS"]
        sites = [(f"{tag}-sites:{module}", 0) for module in modules]
        existing = (f"{tag}:{owner}:ENGINE", 0)
        lookahead = (f"{tag}:{owner}:ADD", 0)
        reads = {
            "dependent": [(f"{tag}-owner:{owner}", 0), existing],
            "unrelated": [lookahead],
        }
        options = {"path": path, "dialect_reads": reads}
        rename = before.replace("'ENGINE'", "'ENGINE_MUT'")
        self.assertIsNone(self.select(before, rename, **options))
        self.assertEqual(self.select(before, rename, collection=sites, **options), {4})
        lookahead_change = before.replace("'HEAP'", "'ADD'")
        self.assertEqual(self.select(before, lookahead_change, collection=sites, **options), {8})
        self.assertIsNone(self.select(before, lookahead_change, collection=[*sites, lookahead], **options))

    def test_alter_tables_use_independent_lookup_receipts(self):
        path = "sqlglot/parser.py"
        handler = "lambda self: self.parse_session()"
        inner_handler = "lambda self: self.parse_alter_session()"
        before = (
            "class Parser:\n"
            f"    ALTER_PARSERS = {{'SESSION': {handler}}}\n"
            f"    ALTER_ALTER_PARSERS = {{'SESSION': {inner_handler}}}\n"
        )
        owner = "sqlglot.parser.Parser"
        alter_tag, alter_modules = FUNCTION_TABLES["ALTER_PARSERS"]
        inner_tag, inner_modules = FUNCTION_TABLES["ALTER_ALTER_PARSERS"]
        alter_sites = [(f"{alter_tag}-sites:{module}", 0) for module in alter_modules]
        inner_sites = [(f"{inner_tag}-sites:{module}", 0) for module in inner_modules]
        missed_lookup = (f"{alter_tag}:{owner}:MODE", 0)
        reads = {
            "dependent": [(f"{alter_tag}-owner:{owner}", 0), (f"{alter_tag}:{owner}:SESSION", 0)],
            "unrelated": [
                (f"{inner_tag}-owner:{owner}", 0),
                (f"{inner_tag}:{owner}:SESSION", 0),
                missed_lookup,
            ],
        }
        options = {"path": path, "dialect_reads": reads}
        outer = before.replace(f"'SESSION': {handler}", f"'SESSIOX': {handler}")
        inner = before.replace(f"'SESSION': {inner_handler}", f"'SESSIOX': {inner_handler}")
        self.assertIsNone(self.select(before, outer, collection=inner_sites, **options))
        self.assertIsNone(self.select(before, inner, collection=alter_sites, **options))
        self.assertEqual(self.select(before, outer, collection=[*alter_sites, *inner_sites], **options), {4})
        self.assertEqual(self.select(before, inner, collection=[*alter_sites, *inner_sites], **options), {8})
        alias = before.replace(f"'SESSION': {handler}", f"'SESSION': {handler}, 'MODE': {handler}")
        self.assertEqual(self.select(before, alias, collection=alter_sites, **options), {8})
        self.assertIsNone(self.select(before, alias, collection=[*alter_sites, missed_lookup], **options))

    def test_matched_parser_tables_use_existing_handlers_for_additions(self):
        path = "sqlglot/parsers/example.py"
        handler = "lambda self: self.parse_property()"
        owner = "sqlglot.parsers.example.Example"
        for field in MATCHED_TABLE_SITES:
            with self.subTest(field=field):
                before = (
                    "from sqlglot import exp\n"
                    "class Example:\n"
                    f"    {field} = {{**Base.{field}, 'KEEP': {handler}}}\n"
                )
                tag, modules = FUNCTION_TABLES[field]
                sites = [(f"{tag}-sites:{module}", 0) for module in modules]
                reads = {
                    "dependent": [(f"{tag}-owner:{owner}", 0), (f"{tag}:{owner}:KEEP", 0)],
                    "unrelated": [(f"{tag}:{owner}:NEW", 0)],
                }
                options = {"path": path, "collection": sites, "dialect_reads": reads}
                removed = before.replace(f", 'KEEP': {handler}", "")
                self.assertEqual(self.select(before, removed, **options), {4})
                addition = before.replace(f"'KEEP': {handler}", f"'KEEP': {handler}, 'NEW': {handler}")
                self.assertEqual(self.select(before, addition, **options), {8})
                fresh_body = addition.replace(f"'NEW': {handler}", "'NEW': lambda self: self.other_property()")
                self.assertIsNone(self.select(before, fresh_body, **options))
                eager_default = addition.replace(f"'NEW': {handler}", "'NEW': lambda self, value=load(): value")
                self.assertIsNone(self.select(before, eager_default, **options))
                argument_builder = addition.replace(f"'NEW': {handler}", "'NEW': exp.CurrentTime.from_arg_list")
                self.assertIsNone(self.select(before, argument_builder, function_builders=("CurrentTime",), **options))

    def test_function_builder_accepts_index_change_in_plain_constructor(self):
        path = "sqlglot/parsers/example.py"
        before = (
            "from sqlglot import expressions as exp\n"
            "from sqlglot.helper import seq_get\n"
            "class Example:\n"
            "    FUNCTIONS = {'SPLIT': lambda args: exp.RegexpSplit(this=seq_get(args, 0))}\n"
        )
        owner = "sqlglot.parsers.example.Example"
        tag, modules = FUNCTION_TABLES["FUNCTIONS"]
        sites = [(f"{tag}-sites:{module}", 0) for module in modules]
        reads = {"dependent": [(f"{tag}-owner:{owner}", 0)], "unrelated": [(f"{tag}:{owner}:SPLIT", 0)]}
        changed = before.replace("args, 0", "args, 1")
        options = {"path": path, "collection": sites, "dialect_reads": reads}
        self.assertEqual(self.select(before, changed, function_builders={"RegexpSplit": True}, **options), {8})
        self.assertIsNone(self.select(before, changed, function_builders={"RegexpSplit": False}, **options))
        different = changed.replace("seq_get(args, 1)", "modify_globals()")
        self.assertIsNone(self.select(before, different, function_builders={"RegexpSplit": True}, **options))

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

        path = "sqlglot/parser.py"
        self.assertEqual(self.select(
            before, after, collection=[(f"@guard-site:{path}:2:7", 0)], path=path,
            guard_reads={"unrelated": [(f"@guard-read:{path}:2:7", 0)]},
        ), {8})

        importing = before.replace("        return", "        import external\n        return")
        self.assertIsNone(self.select(
            importing, importing.replace("and", "or"), path=path,
            collection=[(f"@guard-site:{path}:2:7", 0)],
            guard_reads={"unrelated": [(f"@guard-read:{path}:2:7", 0)]},
        ))

    def test_equality_guard_selects_tests_that_reach_elif(self):
        path = "sqlglot/generators/example.py"
        before = "def check(kind):\n    if not kind:\n        return None\n    elif kind == 'TABLE':\n        return kind\n"
        after = before.replace("==", "!=")
        registration = (f"@guard-site:{path}:4:9", 0)
        read = (f"@guard-read:{path}:4:9", 0)
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, guard_reads={"unrelated": [read]},
        ), {8})
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, guard_reads={},
        ), set())
        self.assertEqual(self.select(
            before, after.replace("TABLE", "VIEW"), collection=[registration], path=path, guard_reads={},
        ), {4})

    def test_leading_not_selects_tests_that_reach_the_guard(self):
        path = "sqlglot/generators/example.py"
        before = "def check(ready, value):\n    if ready:\n        if value:\n            return value\n"
        after = before.replace("if value:", "if not value:")
        site = f"{path}:3:11"
        registration = (f"@guard-site:{site}", 0)
        read = (f"@guard-read:{site}", 0)
        reads = {"unrelated": [read]}
        for old, new in ((before, after), (after, before)):
            self.assertEqual(self.select(
                old, new, collection=[registration], path=path, guard_reads=reads,
            ), {8})
        self.assertEqual(self.select(
            before, after.replace("not value", "not ready"),
            collection=[registration], path=path, guard_reads=reads,
        ), {4})
        combined = before.replace("if value:", "if value and ready:")
        self.assertEqual(self.select(
            combined, combined.replace("value and ready", "not (value or ready)"),
            collection=[registration], path=path, guard_reads=reads,
        ), {4})

    def test_function_edit_changing_lazy_import_falls_back(self):
        before = "def check():\n    return True\n"
        after = before.replace("    return", "    import external\n    return")
        self.assertIsNone(self.select(before, after))
        self.assertIsNone(self.select(after, before))

    def test_ordered_comparison_uses_boundary_and_keeps_unknown_callers(self):
        path = "sqlglot/generators/example.py"
        before = "def check(version):\n    return version < (23, 12)\n"
        after = before.replace("<", "<=")
        site = f"{path}:2:11"
        registration = (f"@ordered-site:{site}", 0)
        boundary = (f"@ordered-boundary:{site}", 0)
        unsafe = (f"@ordered-unsafe:{site}", 0)
        options = {"path": path, "collection": [registration]}
        self.assertEqual(self.select(before, after, **options), set())
        self.assertEqual(self.select(before, after, ordered_reads={"unrelated": [boundary]}, **options), {8})
        self.assertEqual(self.select(before, after, ordered_reads={"unrelated": [unsafe]}, **options), {4, 8})
        self.assertEqual(self.select(before, after.replace("23", "24"), **options), {4})
        self.assertIsNone(self.select(before, after, collection=[registration, boundary], path=path))

    def test_ordered_threshold_edit_selects_operands_whose_result_changes(self):
        path = "sqlglot/generators/example.py"
        before = "def check(value):\n    return value > 29\n"
        after = before.replace("29", "30")
        site = f"{path}:2:11"
        registration = [(f"@ordered-site:{site}", 2), (f"@ordered-value-site:{site}", 0)]
        boundary = (f"@ordered-value:{site}:0x1d", 0)
        crosses = (f"@ordered-value:{site}:0x1e", 0)
        unsafe = (f"@ordered-unsafe:{site}", 0)
        reads = {"dependent": [boundary], "unrelated": [crosses]}
        self.assertEqual(self.select(before, after, path=path, collection=registration, ordered_reads=reads), {8})
        self.assertEqual(
            self.select(before, after.replace(">", ">="), path=path, collection=registration, ordered_reads=reads),
            set(),
        )
        self.assertEqual(self.select(before, after, path=path, collection=registration[:1], ordered_reads=reads), {4})
        self.assertIsNone(self.select(before, after, path=path, collection=[*registration, crosses]))
        for reader in ((path, 0), (f"@identity-code:{path}:1", 0)):
            self.assertIsNone(self.select(before, after, path=path, collection=[*registration, reader]))
        reads["unrelated"].append(unsafe)
        self.assertEqual(self.select(before, after, path=path, collection=registration, ordered_reads=reads), {4, 8})

    def test_ordered_threshold_on_left_replays_signed_tuples(self):
        path = "sqlglot/generators/example.py"
        before = "def check(version):\n    return (\n        -3, 0,\n    ) > version\n"
        after = before.replace("    )", "        1,\n    )")
        site = f"{path}:2:11"
        registration = [(f"@ordered-site:{site}", 4), (f"@ordered-value-site:{site}", 0)]
        reads = {"dependent": set(), "unrelated": set()}
        self.assertFalse(observe_order(reads["unrelated"], (-3, 0), (-3, 0), "gt", site, 0))
        self.assertTrue(observe_order(reads["dependent"], (-3, 0), (), "gt", site, 0))
        self.assertEqual(self.select(before, after, path=path, collection=registration, ordered_reads=reads), {8})

    def test_ordered_comparison_scan_skips_other_lines_in_its_function(self):
        path = "sqlglot/generators/example.py"
        before = "def check(value):\n    value = value + 1\n    return value > 29\n"
        after = before.replace("+ 1", "+ 2")
        with patch("impact.changed_ordered_sites") as scan:
            self.assertEqual(self.select(before, after, path=path, collection=[(f"@ordered-site:{path}:3:11", 3)]), {4})
        scan.assert_not_called()

    def test_ordered_comparison_keeps_code_readers_when_edit_starts_outside_span(self):
        path = "sqlglot/generators/example.py"
        before = "def check(value):\n    return (\n        # bound\n        3 > value\n    )\n"
        after = before.replace("# bound", "-")
        site = f"{path}:4:8"
        registration = [(f"@ordered-site:{site}", 4), (f"@ordered-value-site:{site}", 0)]
        reader = (f"@identity-code:{path}:1", 0)
        self.assertEqual(
            self.select(before, after, path=path, collection=registration, ordered_reads={"unrelated": [reader]}),
            {8},
        )
        self.assertIsNone(self.select(before, after, path=path, collection=[*registration, reader]))

    def test_ordered_probe_preserves_original_comparison(self):
        calls = []
        result = object()

        class CustomTuple(tuple):
            def __lt__(self, other):
                calls.append("lt")
                return result

            def __eq__(self, other):
                calls.append("eq")
                return False

        scope = set()
        self.assertFalse(observe_order(scope, (23, 12, 0), (23, 12), "lt", "site"))
        self.assertFalse(observe_order(scope, (23, 12, 0), (23, 12), "le", "site"))
        self.assertEqual(scope, set())
        self.assertTrue(observe_order(scope, 3, 3, "le", "site"))
        self.assertIs(observe_order(scope, CustomTuple((23, 12)), (23, 12), "lt", "site"), result)
        self.assertEqual(calls, ["lt"])
        self.assertEqual(scope, {("@ordered-boundary:site", 0), ("@ordered-unsafe:site", 0)})

    def test_local_literal_selects_executed_assignments(self):
        path = "sqlglot/generators/example.py"
        before = "def check():\n    value = True\n    return value\n"
        after = before.replace("True", "False")
        registration = (f"@literal-site:{path}:2:4", 0)
        read = (f"@literal-read:{path}:2:4", 0)
        reads = {"unrelated": [read]}
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, literal_reads=reads,
        ), {8})
        self.assertEqual(self.select(
            before, before.replace("True", "0"), collection=[registration],
            path=path, literal_reads=reads,
        ), {4})
        self.assertIsNone(self.select(
            before, after, collection=[registration, read], path=path, literal_reads=reads,
        ))

        shared = before.replace("    value", "    global value\n    value", 1)
        self.assertEqual(self.select(
            shared, shared.replace("True", "False"), collection=[(f"@literal-site:{path}:3:4", 0)],
            path=path, literal_reads={"unrelated": [(f"@literal-read:{path}:3:4", 0)]},
        ), {4})

    def test_local_literal_selects_executed_return(self):
        path = "sqlglot/generators/example.py"
        before = "def check(flag):\n    if flag:\n        return 'yes'\n    return 'no'\n"
        after = before.replace("'yes'", "'maybe'")
        registration = (f"@literal-site:{path}:3:8", 0)
        reads = {"unrelated": [(f"@literal-read:{path}:3:8", 0)]}
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, literal_reads=reads,
        ), {8})
        self.assertEqual(self.select(
            before, before.replace("'yes'", "1"), collection=[registration], path=path, literal_reads=reads,
        ), {4})

    def test_local_literal_selects_executed_return_call(self):
        path = "sqlglot/generators/example.py"
        before = "def render(self, value):\n    return self.func('COALESCE', value, quoted=False)\n"
        after = before.replace("'COALESCE'", "'IFNULL'").replace("False", "True")
        registration = (f"@literal-site:{path}:2:4", 0)
        reads = {"unrelated": [(f"@literal-read:{path}:2:4", 0)]}
        self.assertEqual(self.select(
            before, after, collection=[registration], path=path, literal_reads=reads,
        ), {8})
        self.assertEqual(self.select(
            before, after.replace("self.func", "self.other"),
            collection=[registration], path=path, literal_reads=reads,
        ), {4})
