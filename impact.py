"""Select tests using dependencies observed on the pinned baseline."""

import ast
import gzip
import json
import re
from collections import defaultdict

from factory_changes import factory_argument_structure

FILE_HEADER = re.compile(
    r"a/([\w./-]+) b/\1\n(?:index [^\n]*\n)?--- a/\1\n\+\+\+ b/\1\n(?=@@ )",
    re.ASCII,
)
HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@[^\n]*\n")
UNTRACED = ("*", 0)


def apply_hunks(source, diff):
    """Apply ordinary text hunks to the original source, checking old context."""
    original = source.splitlines(keepends=True)
    updated = []
    position = 0
    for hunk in re.split(r"(?m)^(?=@@ )", diff)[1:]:
        header = HUNK_HEADER.match(hunk)
        if not header:
            raise ValueError("invalid hunk header")
        start = int(header[1])
        count = int(header[2]) if header[2] is not None else 1
        new_count = int(header[3]) if header[3] is not None else 1
        start -= bool(count)
        if not position <= start <= start + count <= len(original):
            raise ValueError("invalid hunk range")

        updated.extend(original[position:start])
        position = start
        updated_before_hunk = len(updated)
        for line in hunk[header.end():].splitlines(keepends=True):
            if line[0] in " -":
                if position >= start + count or original[position] != line[1:]:
                    raise ValueError("hunk does not match baseline")
                position += 1
            if line[0] in " +":
                updated.append(line[1:])
            if line[0] not in " +-":
                raise ValueError("unsupported hunk line")
        if position != start + count or len(updated) - updated_before_hunk != new_count:
            raise ValueError("incomplete hunk")

    updated.extend(original[position:])
    return "".join(updated)


class FunctionBodies(ast.NodeVisitor):
    def __init__(self, source, filename):
        # Python's loader parses bytes, including any source encoding declaration.
        encoded = source.encode("utf-8")
        module = compile(encoded, filename, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
        compile(module, filename, "exec", dont_inherit=True)
        self.bodies = []
        self.visit(module)
        self.structure = ast.dump(module)

    def visit_FunctionDef(self, node):
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        self.bodies.append((start, node.end_lineno, body))
        # Defaults, decorators, and docstrings can affect import-time behavior.
        node.body = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        # A class-scope closure can change how the class is built at import time.
        if any(
            isinstance(value, ast.Name) and value.id in {"__class__", "super"}
            for value in ast.walk(node.body)
        ):
            return
        self.bodies.append((node.lineno, node.end_lineno, ast.dump(node.body)))
        node.body = ast.Constant(value=None)


class Impact:
    def __init__(self, tests, coverage_file, baseline, import_map_file=None):
        self.baseline = baseline
        self.baseline_files = {}
        with gzip.open(coverage_file, "rt", encoding="utf-8") as stream:
            coverage = json.load(stream)
        self.collection = set(map(tuple, coverage["collection"]))
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
                for path, line in dependencies:
                    self.dependents[path, line].add(test["number"])

        self.lines = defaultdict(set)
        for path, line in self.dependents.keys() | self.collection:
            if line:
                self.lines[path].add(line)

    def baseline_file(self, path):
        if path not in self.baseline_files:
            source = (self.baseline / path).read_bytes().decode("utf-8")
            self.baseline_files[path] = source, FunctionBodies(source, path)
        return self.baseline_files[path]

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

    def select(self, patch):
        """Return affected test numbers, or None when the full suite is needed."""
        changes = re.split(r"(?m)^diff --git ", patch)
        if len(changes) < 2 or changes[0]:
            return None

        selected = self.uncovered | self.dependents.get(UNTRACED, set())
        for change in changes[1:]:
            header = FILE_HEADER.match(change)
            if not header:
                return None
            path = header[1]
            if any(part in {"", ".", ".."} for part in path.split("/")):
                return None

            dependencies = {(path, 0)}
            if path.startswith("sqlglot/") and path.endswith(".py"):
                try:
                    source, before = self.baseline_file(path)
                    updated = apply_hunks(source, change[header.end():])
                    after = FunctionBodies(updated, path)
                    if before.structure != after.structure:
                        if len(changes) == 2 and self.is_parser_factory_edit(path, source, updated):
                            return selected | self.tests_for_module(path)
                        return None
                except (OSError, ValueError, SyntaxError, RecursionError):
                    return None
                for (start, end, body), (_, _, new_body) in zip(before.bodies, after.bodies):
                    if body != new_body:
                        dependencies.update((path, line) for line in self.lines[path] if start <= line <= end)
                if dependencies & self.collection:
                    return None
            elif path.startswith("tests/fixtures/") and (path, 0) in self.dependents:
                if (path, 0) in self.collection:
                    return None
            else:
                return None

            for dependency in dependencies:
                selected.update(self.dependents.get(dependency, ()))
        return selected
