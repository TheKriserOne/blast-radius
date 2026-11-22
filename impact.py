"""Select tests using dependencies observed on the pinned baseline."""

import ast
import gzip
import json
import marshal
import re
from collections import defaultdict
from hashlib import sha256
from types import SimpleNamespace
from zipfile import BadZipFile, ZipFile

from dialect_fields import TYPE_MEMBERS_SOURCE, changed_dialect_fields, changed_type_mappings
from expression_arguments import changed_expression_validators
from factory_changes import FUNCTION_BUILDERS_SOURCE, changed_function_keys, factory_argument_structure
from factory_probe import FUNCTION_TABLES, TOKEN_MATCH_TABLE_SITES, TOKEN_MEMBERS_SOURCE, changed_show_entries
from factory_probe import opaque as factory_opaque, tag as factory_tag
from fixture_reads import filter_comments, filtered_dependency, fixture_pairs, pair_dependency
from guard_comparisons import changed_guards, supports_guard_profile
from identity_comparisons import changed_identity_sites, supports_identity_profile
from literal_statements import changed_local_literals, supports_literal_profile
from module_bindings import binding_read_lines
from ordered_comparisons import changed_ordered_sites, threshold_changes_result
from syntax_profile import PROFILE_DIRECTORY, code_at_lines, equivalent_module

FILE_HEADER = re.compile(
    r"a/([\w./-]+) b/\1\n(?:index [^\n]*\n)?--- a/\1\n\+\+\+ b/\1\n(?=@@ )",
    re.ASCII,
)
HUNK_HEADER = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@[^\n]*\n")
CHANGED_COMMENT = re.compile(r"(?m)^[+-][^\n]*#")
NO_NEWLINE = "\\ No newline at end of file\n"
UNTRACED = ("*", 0)
# The pinned suite also has no file metadata or cached-bytecode consumers for this module.
COMMENT_SAFE_MODULES = {"sqlglot/helper.py"}


def mask_guard_operators(body):
    """Coarse normalization used only to reject impossible guard-only edits."""
    # Removing closing parentheses also masks the extra parenthesis around a leading not.
    return (body.replace("BoolOp(op=Or()", "BoolOp(op=And()").replace("ops=[NotEq()]", "ops=[Eq()]")
            .replace("UnaryOp(op=Not(), operand=", "").replace(")", ""))


def physical_lines(text):
    """Keep Git's LF delimiters and leave other line separator bytes intact."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def hunk_lines(body):
    """Read each hunk row with its optional Git end-of-file annotation."""
    lines = iter(physical_lines(body))
    line = next(lines, None)
    while line is not None:
        if line[0] not in " +-" or not line.endswith("\n"):
            raise ValueError("unsupported hunk line")
        following = next(lines, None)
        if following == NO_NEWLINE:
            if line == line[0] + "\n":
                raise ValueError("empty line with no newline")
            line = line[:-1]
            following = next(lines, None)
        yield line[0], line[1:]
        line = following


def apply_hunks(source, diff, touched_lines=None):
    """Apply Git text hunks to the original source, checking old context."""
    original = physical_lines(source)
    updated = []
    position = 0
    prefix, *hunks = re.split(r"(?m)^(?=@@ )", diff)
    if prefix or not hunks:
        raise ValueError("expected Git hunks")
    for hunk in hunks:
        header = HUNK_HEADER.match(hunk)
        if not header:
            raise ValueError("invalid hunk header")
        start, count, new_start, new_count = map(int, header.groups(default="1"))
        start -= bool(count)
        new_start -= bool(new_count)
        if not position <= start <= start + count <= len(original):
            raise ValueError("invalid hunk range")

        updated.extend(original[position:start])
        if new_start != len(updated):
            raise ValueError("invalid new hunk range")
        position = start
        previous_operation = " "
        for operation, line in hunk_lines(hunk[header.end():]):
            if touched_lines is not None:
                if operation == "-":
                    touched_lines.add(position + 1)
                elif operation == "+" and previous_operation == " ":
                    # Insertions can extend an expression on either side.
                    touched_lines.update((position, position + 1))
            previous_operation = operation
            if operation in " -":
                if position >= start + count or original[position] != line:
                    raise ValueError("hunk does not match baseline")
                position += 1
            if operation in " +":
                updated.append(line)
        if position != start + count or len(updated) != new_start + new_count:
            raise ValueError("incomplete hunk")

    updated.extend(original[position:])
    if any(not line.endswith("\n") for line in updated[:-1]):
        raise ValueError("missing newline before end of file")
    return "".join(updated)


class FunctionBodies(ast.NodeVisitor):
    def __init__(self, source, filename):
        # Python's loader parses bytes, including any source encoding declaration.
        encoded = source.encode("utf-8")
        module = compile(encoded, filename, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
        compile(module, filename, "exec", dont_inherit=True)
        self.bodies = []
        self.has_parser_tables = filename == "sqlglot/parser.py" or filename.startswith("sqlglot/parsers/")
        self.visit(module)
        self.structure = ast.dump(module)

    def visit_FunctionDef(self, node):
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        imports = ("Import(" in body or "ImportFrom(" in body) and any(
            isinstance(part, (ast.Import, ast.ImportFrom)) for part in ast.walk(node)
        )
        self.bodies.append((start, node.end_lineno, body, imports))
        # Defaults, decorators, and docstrings can affect import-time behavior.
        node.body = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        # Token-keyed handler bodies retain ordinary line coverage.
        if self.has_parser_tables and any(
            isinstance(target, ast.Name) and target.id in FUNCTION_TABLES and target.id not in TOKEN_MATCH_TABLE_SITES
            for target in node.targets
        ):
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        target = node.target
        if (
            self.has_parser_tables and isinstance(target, ast.Name)
            and target.id in FUNCTION_TABLES and target.id not in TOKEN_MATCH_TABLE_SITES
        ):
            return
        self.generic_visit(node)

    def visit_Lambda(self, node):
        # A class-scope closure can change how the class is built at import time.
        if any(
            isinstance(value, ast.Name) and value.id in {"__class__", "super"}
            for value in ast.walk(node.body)
        ):
            return
        self.bodies.append((node.lineno, node.end_lineno, ast.dump(node.body), False))
        node.body = ast.Constant(value=None)


class Impact:
    def __init__(self, tests, coverage_file, baseline, import_map_file=None):
        self.baseline = baseline
        self.baseline_files = {}
        self.token_members = None
        try:
            self.baseline_syntax = ZipFile(baseline / "syntax.zip")
        except (OSError, BadZipFile):
            self.baseline_syntax = None
        with gzip.open(coverage_file, "rt", encoding="utf-8") as stream:
            coverage = json.load(stream)
        self.has_expression_validation = coverage.get("expression_validation", False)
        self.has_argument_presence = coverage.get("argument_presence", False)
        self.has_dialect_fields = coverage.get("dialect_fields", False)
        self.has_identity_comparisons = coverage.get("identity_comparisons", False)
        self.has_guard_coverage = coverage.get("guard_coverage", False)
        self.has_literal_coverage = coverage.get("literal_coverage", False)
        self.has_factory_calls = coverage.get("factory_calls", False)
        self.has_explicit_source_reads = coverage.get("explicit_source_reads") is True
        decode_dependency = tuple
        if "dependencies" in coverage:
            dependency_table = [tuple(entry) for entry in coverage["dependencies"]]
            decode_dependency = dependency_table.__getitem__
        self.collection = set(map(decode_dependency, coverage["collection"]))
        self.collection_files = {path for path, _ in self.collection}

        self.import_map = {"factories": {}, "files_by_path": {}}
        self.tests_by_file = defaultdict(set)
        self.files_by_number = {}
        self.unprofiled = set()
        if import_map_file is not None:
            with gzip.open(import_map_file, "rt", encoding="utf-8") as stream:
                self.import_map = json.load(stream)
            complete = set(self.import_map["complete_files"])
            for test in tests:
                file, number = test["file"], test["number"]
                self.tests_by_file[file].add(number)
                self.files_by_number[number] = file
                if file not in complete:
                    self.unprofiled.add(number)

        self.dependents = defaultdict(set)
        self.uncovered = set()
        for test in tests:
            dependencies = coverage["tests"].get(test["id"])
            if dependencies is None:
                self.uncovered.add(test["number"])
            else:
                for dependency in map(decode_dependency, dependencies):
                    self.dependents[dependency].add(test["number"])
        for alias, representative in coverage.get("aliases", ()):
            tests_for_dependency = self.dependents.get(decode_dependency(representative))
            if tests_for_dependency is not None:
                self.dependents[decode_dependency(alias)] = tests_for_dependency

        self.lines = defaultdict(set)
        self.ordered_sites = set()
        self.ordered_spans = defaultdict(list)
        self.ordered_value_sites = set()
        self.ordered_values = defaultdict(list)
        self.code_reads = defaultdict(set)
        self.unknown_function_reads = {field: set() for field in FUNCTION_TABLES}
        unknown_function_fields = {f"{tag}-unknown": field for field, (tag, _) in FUNCTION_TABLES.items()}
        all_dependencies = self.dependents.keys() | self.collection
        for path, line in all_dependencies:
            if "-unknown:" in path:
                field = unknown_function_fields.get(path.partition(":")[0])
                if field is not None:
                    self.unknown_function_reads[field].add((path, line))
            if path.startswith("@ordered-site:"):
                site = path.removeprefix("@ordered-site:")
                file, lineno, _ = site.rsplit(":", 2)
                self.ordered_sites.add(site)
                self.ordered_spans[file].append((int(lineno), line))
            elif line:
                self.lines[path].add(line)
            elif path.startswith("@ordered-value-site:"):
                self.ordered_value_sites.add(path.removeprefix("@ordered-value-site:"))
            elif path.startswith("@ordered-value:"):
                site, token = path.removeprefix("@ordered-value:").rsplit(":", 1)
                self.ordered_values[site].append((token, (path, line)))
            elif path.startswith("@identity-code:"):
                file, _ = path.removeprefix("@identity-code:").rsplit(":", 1)
                self.code_reads[file].add((path, line))
        self.unknown_numeric_reads = {
            dependency for dependency in all_dependencies if dependency[0].startswith("@numeric-unknown:")
        }
        self.has_numeric_missing_keys = ("@numeric-missing-keys", 0) in all_dependencies
        self.has_type_mapping_reads = {
            ("@type-map-sites:sqlglot.generator", 0),
            ("@type-map-sites:sqlglot.expressions.builders", 0),
        } <= all_dependencies
        self.covered_function_tables = {
            field for field, (tag, modules) in FUNCTION_TABLES.items()
            if {(f"{tag}-sites:{module}", 0) for module in modules} <= all_dependencies
        }
        self.identity_sites = {
            path.removeprefix("@identity-site:") for path, _ in all_dependencies
            if path.startswith("@identity-site:")
        }
        self.guard_sites = {
            path.removeprefix("@guard-site:") for path, _ in all_dependencies
            if path.startswith("@guard-site:")
        }
        self.literal_sites = {
            path.removeprefix("@literal-site:") for path, _ in all_dependencies
            if path.startswith("@literal-site:")
        }
        self.factory_evidence = {
            dep for dep in all_dependencies if dep[0].startswith("@factory:")
        }

    def baseline_file(self, path):
        if path not in self.baseline_files:
            raw = (self.baseline / path).read_bytes()
            source = raw.decode("utf-8")
            summary = None
            if self.baseline_syntax is not None:
                try:
                    digest, structure, bodies = marshal.loads(self.baseline_syntax.read(path))
                except (BadZipFile, EOFError, KeyError, ValueError):
                    pass
                else:
                    if digest == sha256(raw).digest():
                        summary = SimpleNamespace(structure=structure, bodies=bodies)
            self.baseline_files[path] = source, summary or FunctionBodies(source, path)
        return self.baseline_files[path]

    def cached_profile(self, kind, path, source):
        if self.baseline_syntax is None:
            return None
        try:
            digest, profile = marshal.loads(self.baseline_syntax.read(f"{PROFILE_DIRECTORY}/{kind}/{path}"))
        except (BadZipFile, EOFError, KeyError, ValueError):
            return None
        if digest == sha256(source.encode("utf-8")).digest():
            return profile

    def is_parser_factory_edit(self, path, source, updated):
        factories = self.import_map["factories"].get(path)
        if not factories or path in self.collection_files or not self.import_map["files_by_path"].get(path):
            return False
        before = factory_argument_structure(ast.parse(source.encode("utf-8"), path), factories)
        if before is None:
            return False
        after = factory_argument_structure(ast.parse(updated.encode("utf-8"), path), factories)
        return before == after

    def tests_for_module(self, path):
        files = set(self.import_map["files_by_path"][path])
        for line in self.lines[path] | {0}:
            files.update(self.files_by_number[number]
                         for number in self.dependents.get((path, line), ()))
        tests = self.unprofiled.copy()
        for file in files:
            tests.update(self.tests_by_file[file])
        return tests

    def tests_for_expression_arguments(self, path, source, updated):
        if (
            not self.has_expression_validation
            or not path.startswith("sqlglot/expressions/")
            or (path, 0) in self.collection
        ):
            return None
        validators, additions = changed_expression_validators(path, source, updated)
        if not validators or validators & self.collection or not validators <= self.dependents.keys():
            return None
        if self.has_argument_presence and additions:
            classes = {tag.removeprefix("@expr:") for tag, _ in validators}
            validators = {(f"@expr-opaque:{name}", 0) for name in classes} | {
                (f"@expr-missing:{name}:{key}", 0) for name in classes for key in additions
            }
        tests = set(self.dependents.get((path, 0), ()))
        for validator in validators:
            tests.update(self.dependents.get(validator, ()))
        return tests

    def tests_for_dialect_fields(self, path, source, updated):
        if not self.has_dialect_fields or not path.startswith("sqlglot/dialects/"):
            return None
        fields, added = changed_dialect_fields(path, source, updated)
        if not fields or (added and not self.has_numeric_missing_keys):
            return None
        dependencies = {(path, 0)}
        for field in fields:
            dependencies.add((field, 0))
            if field.startswith("@numeric:"):
                _, owner, key = field.split(":", 2)
                observed_owner = (f"@numeric-owner:{owner}", 0)
                if observed_owner not in self.dependents and observed_owner not in self.collection:
                    return None
                dependencies.update(self.unknown_numeric_reads)
                if field in added:
                    dependencies.add((f"@numeric-miss:{owner}:{key}", 0))
                    dependencies.add((f"@numeric-unknown-miss:{key}", 0))
            elif (field, 0) not in self.dependents:
                return None
        if dependencies & self.collection:
            return None
        return set().union(*(self.dependents.get(dependency, ()) for dependency in dependencies))

    def type_mapping_dependencies(self, path, source, updated):
        if not self.has_dialect_fields or not self.has_type_mapping_reads or not (
            path == "sqlglot/generator.py" or path.startswith("sqlglot/generators/")
        ):
            return None
        fields, added = changed_type_mappings(path, source, updated)
        if not fields:
            return None
        if added:
            enum_source = (self.baseline / TYPE_MEMBERS_SOURCE).read_bytes().decode("utf-8")
            members = self.cached_profile("type-members", TYPE_MEMBERS_SOURCE, enum_source)
            if members is None or not added.issubset(members):
                return None
        for field in fields:
            _, owner, _ = field.split(":", 2)
            marker = (f"@type-map-owner:{owner}", 0)
            if marker not in self.dependents and marker not in self.collection:
                return None
        return {(path, 0), ("@type-map-unknown", 0)} | self.code_reads.get(path, set()) | {
            (field, 0) for field in fields
        }

    def identity_dependencies(self, path, source, updated):
        if not self.has_identity_comparisons or not supports_identity_profile(path.replace("/", ".")):
            return None
        sites, code_lines = changed_identity_sites(path, source, updated, self.cached_profile("identity", path, source))
        if not sites or not sites <= self.identity_sites:
            return None
        return (
            {(path, 0)}
            | {(f"@identity-code:{path}:{line}", 0) for line in code_lines}
            | {(f"@identity-unsafe:{site}", 0) for site in sites}
        )

    def function_table_dependencies(self, path, source, updated):
        if not self.has_dialect_fields or not self.covered_function_tables or not (
            path == "sqlglot/parser.py" or path.startswith("sqlglot/parsers/")
        ):
            return None
        builders = {}
        registry = self.baseline / FUNCTION_BUILDERS_SOURCE
        if self.baseline_syntax is not None and registry.is_file():
            profile = self.cached_profile("function-builders", FUNCTION_BUILDERS_SOURCE, registry.read_text("utf-8"))
            if isinstance(profile, dict):
                builders = profile
            elif profile:
                builders = dict.fromkeys(profile, False)
        uses_token_table = any(field in source for field in TOKEN_MATCH_TABLE_SITES.keys() & self.covered_function_tables)
        if uses_token_table and self.token_members is None:
            registry = self.baseline / TOKEN_MEMBERS_SOURCE
            profile = None
            if self.baseline_syntax is not None and registry.is_file():
                profile = self.cached_profile("token-members", TOKEN_MEMBERS_SOURCE, registry.read_bytes().decode("utf-8"))
            self.token_members = frozenset(profile or ())
        token_members = self.token_members if uses_token_table else ()
        changes = changed_function_keys(path, source, updated, builders, token_members)
        if not changes:
            return None
        dependencies = {(path, 0)}
        for (field, owner), keys in changes.items():
            if field not in self.covered_function_tables:
                return None
            # Teradata copies this entry under TRYCAST during lazy class initialization.
            if field == "FUNCTION_PARSERS" and owner == "sqlglot.parser.Parser" and "TRY_CAST" in keys:
                return None
            tag, _ = FUNCTION_TABLES[field]
            marker = (f"{tag}-owner:{owner}", 0)
            if marker not in self.dependents and marker not in self.collection:
                return None
            dependencies.add((f"{tag}-unknown", 0))
            dependencies.update(self.unknown_function_reads[field])
            dependencies.update((f"{tag}:{owner}:{key}", 0) for key in keys)
        if path in self.code_reads:
            reads = {tag: int(tag[0].rsplit(":", 1)[1]) for tag in self.code_reads[path]}
            lines = set(reads.values())
            before = code_at_lines(path, source, lines)
            after = code_at_lines(path, updated, lines)
            dependencies.update(tag for tag, line in reads.items() if line not in before or before[line] != after.get(line))
        return dependencies

    def module_binding_dependencies(self, path, source, updated):
        lines = binding_read_lines(path, source, updated)
        if lines is None:
            return None
        return {(path, 0), (f"@identity-code:{path}:1", 0)} | {(path, line) for line in lines}

    def guard_dependencies(self, path, source, updated):
        if not self.has_guard_coverage or not supports_guard_profile(path.removesuffix(".py").replace("/", ".")):
            return None
        sites, code_lines = changed_guards(path, source, updated, self.cached_profile("guard", path, source))
        if not sites or not sites <= self.guard_sites:
            return None
        return (
            {(path, 0)}
            | {(f"@identity-code:{path}:{line}", 0) for line in code_lines}
            | {(f"@guard-read:{site}", 0) for site in sites}
        )

    def ordered_dependencies(self, path, source, updated, changed_ranges, touched_lines):
        if not any(
            any(start <= line <= end for line in touched_lines)
            if end and path not in self.code_reads
            else any(first <= start <= last for first, last in changed_ranges)
            for start, end in self.ordered_spans[path]
        ):
            return None
        changes, code_lines = changed_ordered_sites(path, source, updated, self.cached_profile("ordered", path, source))
        if not changes or not changes.keys() <= self.ordered_sites:
            return None
        unsafe = {(f"@ordered-unsafe:{site}", 0) for site in changes}
        dependencies = (
            {(path, 0)}
            | {(f"@identity-code:{path}:{line}", 0) for line in code_lines}
            | unsafe
        )
        for site, (original, replacement) in changes.items():
            if original[2] == replacement[2]:
                dependencies.add((f"@ordered-boundary:{site}", 0))
            elif site in self.ordered_value_sites:
                dependencies.update(
                    dependency for token, dependency in self.ordered_values.get(site, ())
                    if threshold_changes_result(token, original, replacement)
                )
            else:
                return None
        if any(dependency in self.dependents for dependency in unsafe):
            # Fall back to callers of the changed functions, retaining code readers.
            dependencies.update(
                (path, line)
                for start, end in changed_ranges
                for line in self.lines[path] if start <= line <= end
            )
        return dependencies

    def literal_dependencies(self, path, source, updated):
        if not self.has_literal_coverage or not supports_literal_profile(path.replace("/", ".")):
            return None
        sites, code_lines = changed_local_literals(path, source, updated, self.cached_profile("literal", path, source))
        if not sites or not sites <= self.literal_sites:
            return None
        return (
            {(path, 0)}
            | {(f"@identity-code:{path}:{line}", 0) for line in code_lines}
            | {(f"@literal-read:{site}", 0) for site in sites}
        )

    def factory_call_dependencies(self, path, source, updated):
        if not self.has_factory_calls or factory_opaque(path) in self.factory_evidence:
            return None
        sites = changed_show_entries(path, source, updated, self.import_map["factories"][path])
        if not sites or any(
            factory_tag("registered", path, site) not in self.factory_evidence for site in sites
        ):
            return None
        return {(path, 0)} | {factory_tag("called", path, site) for site in sites}

    def fixture_dependencies(self, path, hunks, isolated):
        raw = (path, 0)
        filtered = filtered_dependency(path)
        pairs = pair_dependency(path)
        observed = {
            dependency for dependency in (raw, filtered, pairs)
            if dependency in self.dependents or dependency in self.collection
        }
        if not observed:
            return None
        if not isolated or observed == {raw}:
            return observed
        try:
            source = (self.baseline / path).read_bytes().decode("utf-8")
            before = filter_comments(source)
            after = filter_comments(apply_hunks(source, hunks))
            if before == after:
                return observed - {filtered, pairs}
            if pairs in observed and tuple(fixture_pairs(before)) == tuple(fixture_pairs(after)):
                observed.remove(pairs)
        except (OSError, ValueError, IndexError):
            pass
        return observed

    def equivalent_source(self, path, hunks):
        raw = (path, 0)
        if (
            not self.has_explicit_source_reads
            or path not in COMMENT_SAFE_MODULES
            or raw in self.collection
            or raw in self.dependents
            or not CHANGED_COMMENT.search(hunks)
        ):
            return False
        try:
            source = (self.baseline / path).read_bytes()
            updated = apply_hunks(source.decode("utf-8"), hunks).encode("utf-8")
            return equivalent_module(path, source, updated)
        except (OSError, ValueError, SyntaxError, RecursionError, Warning):
            return False

    def select(self, patch):
        """Return affected test numbers, or None when the full suite is needed."""
        sections = re.split(r"(?m)^diff --git ", patch)
        if len(sections) < 2 or sections[0]:
            return None

        changes = {}
        for section in sections[1:]:
            header = FILE_HEADER.match(section)
            if not header:
                return None
            path = header[1]
            if path in changes or any(part in {"", ".", ".."} for part in path.split("/")):
                return None
            changes[path] = section[header.end():]

        if len(changes) > 1:
            changes = {path: hunks for path, hunks in changes.items() if not self.equivalent_source(path, hunks)}
            sources = {path: hunks for path, hunks in changes.items()
                       if path.startswith("sqlglot/") and path.endswith(".py")}
            # A fixture with identical loader output and no raw readers adds no effective edit.
            if len(sources) <= 1 and all(
                path in sources or (path.startswith("tests/fixtures/")
                                    and self.fixture_dependencies(path, hunks, True) == set())
                for path, hunks in changes.items()
            ):
                changes = sources

        isolated = len(changes) == 1
        selected = self.uncovered | self.dependents.get(UNTRACED, set())
        for path, hunks in changes.items():
            dependencies = {(path, 0)}
            if path.startswith("sqlglot/") and path.endswith(".py"):
                try:
                    source, before = self.baseline_file(path)
                    touched_lines = set() if isolated and path in self.ordered_spans else None
                    updated = apply_hunks(source, hunks, touched_lines)
                    after = FunctionBodies(updated, path)
                    same_structure = before.structure == after.structure
                    changed_ranges = []
                    possible_guard_edit = self.has_guard_coverage and isolated
                    if same_structure:
                        for (start, end, body, imports), (_, _, new_body, new_imports) in zip(
                            before.bodies, after.bodies,
                        ):
                            if body == new_body:
                                continue
                            # A changed lazy import can affect tests that never call this function.
                            if imports or new_imports:
                                return None
                            changed_ranges.append((start, end))
                            if possible_guard_edit:
                                possible_guard_edit = mask_guard_operators(body) == mask_guard_operators(new_body)
                    if isolated and (changed_ranges or not same_structure):
                        profiled_deps = self.identity_dependencies(path, source, updated)
                        if profiled_deps is None and same_structure and touched_lines:
                            profiled_deps = self.ordered_dependencies(path, source, updated, changed_ranges, touched_lines)
                        if profiled_deps is None and same_structure and possible_guard_edit:
                            profiled_deps = self.guard_dependencies(path, source, updated)
                        if profiled_deps is None and same_structure:
                            profiled_deps = self.literal_dependencies(path, source, updated)
                        if profiled_deps is None and not same_structure:
                            profiled_deps = self.type_mapping_dependencies(path, source, updated)
                        if profiled_deps is None and not same_structure:
                            profiled_deps = self.function_table_dependencies(path, source, updated)
                        if profiled_deps is None and not same_structure:
                            profiled_deps = self.module_binding_dependencies(path, source, updated)
                        if profiled_deps is not None:
                            if profiled_deps & self.collection:
                                return None
                            return selected.union(*(self.dependents.get(d, ()) for d in profiled_deps))
                    if not same_structure:
                        if not isolated:
                            return None
                        expression_tests = self.tests_for_expression_arguments(path, source, updated)
                        if expression_tests is not None:
                            return selected | expression_tests
                        dialect_tests = self.tests_for_dialect_fields(path, source, updated)
                        if dialect_tests is not None:
                            return selected | dialect_tests
                        if self.is_parser_factory_edit(path, source, updated):
                            factory_deps = self.factory_call_dependencies(path, source, updated)
                            if factory_deps is not None:
                                if factory_deps & self.collection:
                                    return None
                                return selected.union(*(self.dependents.get(d, ()) for d in factory_deps))
                            return selected | self.tests_for_module(path)
                        return None
                except (OSError, ValueError, SyntaxError, RecursionError):
                    return None
                for start, end in changed_ranges:
                    dependencies.update((path, line) for line in self.lines[path] if start <= line <= end)
                if dependencies & self.collection:
                    return None
            elif path.startswith("tests/fixtures/"):
                dependencies = self.fixture_dependencies(path, hunks, isolated)
                if dependencies is None or dependencies & self.collection:
                    return None
            else:
                return None

            for dependency in dependencies:
                selected.update(self.dependents.get(dependency, ()))
        return selected
