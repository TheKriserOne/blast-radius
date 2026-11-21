"""Scope Boolean and equality guard edits to tests that reached the condition."""

import ast

from identity_comparisons import enclosing_code_lines, location


def supports_guard_profile(module):
    return module == "sqlglot.parser" or module.startswith(("sqlglot.parsers.", "sqlglot.generators."))


def guard_operator(node):
    if not isinstance(node, ast.If):
        return None
    test = node.test
    if isinstance(test, ast.BoolOp):
        operator = test.op
    elif isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
        operator = test.ops[0]
    else:
        return None
    if any(isinstance(part, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)) for part in ast.walk(test)):
        return None
    return operator


def guard_structure(module, path):
    operators = {}
    pending = [(module, False)]
    while pending:
        node, local_scope = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_scope = not any(
                isinstance(part, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom))
                for part in ast.walk(node)
            )
        elif isinstance(node, ast.ClassDef):
            local_scope = False
        if local_scope and (operator := guard_operator(node)) is not None:
            operators[location(path, node.test)] = type(operator)
            if isinstance(node.test, ast.BoolOp):
                node.test.op = ast.And()
            else:
                node.test.ops[0] = ast.Eq()
        pending.extend((child, local_scope) for child in ast.iter_child_nodes(node))
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
        if guard_operator(node) is None:
            return node
        site = location(self.path, node.test)
        self.sites.add((f"@guard-site:{site}", 0))
        read = ast.Expr(ast.Call(
            func=ast.Name(id="_blast_radius_guard", ctx=ast.Load()),
            args=[ast.Constant(value=site)],
            keywords=[],
        ))
        return [ast.copy_location(read, node), node]
