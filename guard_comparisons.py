"""Scope Boolean guard edits to tests that reached the condition."""

import ast

from identity_comparisons import enclosing_code_lines, location


def supports_guard_profile(module):
    return module.startswith("sqlglot.parsers.")


def boolean_guard(node):
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and not any(
            isinstance(part, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await))
            for part in ast.walk(node.test)
        )
    )


def guard_structure(module, path):
    operators = {}
    for node in ast.walk(module):
        if boolean_guard(node):
            operators[location(path, node.test)] = type(node.test.op)
            node.test.op = ast.And()
    return ast.dump(module), operators


def changed_guards(path, source, updated):
    module = ast.parse(source.encode("utf-8"), path)
    before, old = guard_structure(module, path)
    after, new = guard_structure(ast.parse(updated.encode("utf-8"), path), path)
    if before != after or len(old) != len(new):
        return set(), set()
    changed = {site for (site, operator), replacement in zip(old.items(), new.values())
               if operator is not replacement}
    return changed, enclosing_code_lines(module, changed)


class GuardReads(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.sites = set()

    def visit_If(self, node):
        self.generic_visit(node)
        if not boolean_guard(node):
            return node
        site = location(self.path, node.test)
        self.sites.add((f"@guard-site:{site}", 0))
        read = ast.Expr(ast.Call(
            func=ast.Name(id="_blast_radius_guard", ctx=ast.Load()),
            args=[ast.Constant(value=site)],
            keywords=[],
        ))
        return [ast.copy_location(read, node), node]
