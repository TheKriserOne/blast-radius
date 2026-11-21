"""Scope Boolean, equality, and negated guard edits to their readers."""

import ast

from identity_comparisons import location
from syntax_profile import enclosing_code_lines, match_profile


def supports_guard_profile(module):
    return module == "sqlglot.parser" or module.startswith(("sqlglot.parsers.", "sqlglot.generators."))


def eligible_guard(node):
    return isinstance(node, ast.If) and not any(
        isinstance(part, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await))
        for part in ast.walk(node.test)
    )


class GuardComparisons(ast.NodeTransformer):
    def __init__(self, path, instrument=False):
        self.path = path
        self.instrument = instrument
        self.local_scope = False
        self.operators = {}
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

    def visit_If(self, node):
        self.generic_visit(node)
        if not self.local_scope or not eligible_guard(node):
            return node
        site = location(self.path, node.test)
        if not self.instrument:
            test = node.test
            negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            if negated:
                test = test.operand
            operator = None
            if isinstance(test, ast.BoolOp):
                operator = type(test.op).__name__
                test.op = ast.And()
            elif isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
                operator = type(test.ops[0]).__name__
                test.ops[0] = ast.Eq()
            self.operators[site] = negated, operator
            node.test = test
            return node
        self.sites.add((f"@guard-site:{site}", 0))
        read = ast.Expr(ast.Call(
            func=ast.Name(id="_blast_radius_guard", ctx=ast.Load()),
            args=[ast.Constant(value=site)],
            keywords=[],
        ))
        return [ast.copy_location(read, node), node]


def guard_structure(module, path):
    normalizer = GuardComparisons(path)
    return ast.dump(normalizer.visit(module)), normalizer.operators


def changed_guards(path, source, updated, baseline=None):
    match = match_profile(path, source, updated, guard_structure, baseline)
    if match is None:
        return set(), set()
    pairs, scopes = match
    changed = set()
    for (site, (negated, operator)), (new_negated, new_operator) in pairs:
        if negated != new_negated and operator != new_operator:
            return set(), set()
        if (negated, operator) != (new_negated, new_operator):
            changed.add(site)
    return changed, enclosing_code_lines(scopes, changed)
