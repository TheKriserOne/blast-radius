"""Scope same-type local literal replacements to executed assignments."""

import ast

from identity_comparisons import enclosing_code_lines, location


def supports_literal_profile(module):
    return module.startswith(("sqlglot.generators.", "sqlglot.optimizer.", "sqlglot.parsers."))


class LiteralAssignments(ast.NodeTransformer):
    def __init__(self, path, instrument=False):
        self.path = path
        self.instrument = instrument
        self.local_scope = False
        self.literals = {}
        self.sites = set()

    def visit_FunctionDef(self, node):
        previous = self.local_scope
        self.local_scope = not any(
            isinstance(part, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom))
            for part in ast.walk(node)
        )
        self.generic_visit(node)
        self.local_scope = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.local_scope
        self.local_scope = False
        self.generic_visit(node)
        self.local_scope = previous
        return node

    def visit_Assign(self, node):
        if not (
            self.local_scope
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
        ):
            return node
        site = location(self.path, node)
        if not self.instrument:
            self.literals[site] = node.value.value
            node.value.value = type(node.value.value).__name__
            return node
        self.sites.add((f"@literal-site:{site}", 0))
        read = ast.Expr(ast.Call(
            func=ast.Name(id="_blast_radius_literal", ctx=ast.Load()),
            args=[ast.Constant(value=site)],
            keywords=[],
        ))
        return [ast.copy_location(read, node), node]


def changed_local_literals(path, source, updated):
    before = ast.parse(source.encode("utf-8"), path)
    after = ast.parse(updated.encode("utf-8"), path)
    old, new = LiteralAssignments(path), LiteralAssignments(path)
    old.visit(before)
    new.visit(after)
    if ast.dump(before) != ast.dump(after) or len(old.literals) != len(new.literals):
        return set(), set()
    # repr preserves differences such as positive and negative zero.
    changed = {site for (site, value), new_value in zip(old.literals.items(), new.literals.values())
               if repr(value) != repr(new_value)}
    return changed, enclosing_code_lines(before, changed)
