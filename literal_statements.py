"""Scope local literal edits to tests that reach the changed statements."""

import ast

from identity_comparisons import location
from syntax_profile import enclosing_code_lines, match_profile


def supports_literal_profile(module):
    return module.startswith(("sqlglot.generators.", "sqlglot.optimizer.", "sqlglot.parsers."))


class LiteralStatements(ast.NodeTransformer):
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
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return node
        return self.visit_literals(node, (node.value,))

    def visit_Return(self, node):
        value = node.value
        if isinstance(value, ast.Call):
            return self.visit_literals(node, [*value.args, *(arg.value for arg in value.keywords)])
        return self.visit_literals(node, (value,))

    def visit_literals(self, node, values):
        if not self.local_scope:
            return node
        literals = [value for value in values if isinstance(value, ast.Constant)]
        if not literals:
            return node
        site = location(self.path, node)
        if not self.instrument:
            self.literals[site] = tuple(repr(literal.value) for literal in literals)
            for literal in literals:
                literal.value = type(literal.value).__name__
            return node
        self.sites.add((f"@literal-site:{site}", 0))
        read = ast.Expr(ast.Call(
            func=ast.Name(id="_blast_radius_literal", ctx=ast.Load()),
            args=[ast.Constant(value=site)],
            keywords=[],
        ))
        return [ast.copy_location(read, node), node]


def literal_structure(module, path):
    normalizer = LiteralStatements(path)
    return ast.dump(normalizer.visit(module)), normalizer.literals


def changed_local_literals(path, source, updated, baseline=None):
    match = match_profile(path, source, updated, literal_structure, baseline)
    if match is None:
        return set(), set()
    pairs, scopes = match
    # repr preserves differences such as positive and negative zero.
    changed = {site for (site, value), new_value in pairs if value != new_value}
    return changed, enclosing_code_lines(scopes, changed)
