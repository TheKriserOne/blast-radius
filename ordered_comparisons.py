"""Scope strict/inclusive edits to observed integer comparison boundaries."""

import ast
import operator

from identity_comparisons import enclosing_code_lines, location


OPERATORS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
OPERATOR_NAMES = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}


def supports_ordered_profile(module):
    return module.startswith(("sqlglot.dialects.", "sqlglot.generators.", "sqlglot.parsers."))


def integer_literal(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) is int


def literal_threshold(node):
    return integer_literal(node) or (
        isinstance(node, ast.Tuple) and all(integer_literal(item) for item in node.elts)
    )


class OrderedComparisons(ast.NodeTransformer):
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
        # Defaults, decorators, and annotations can evaluate during import.
        node.body = [self.visit(statement) for statement in node.body]
        self.local_scope = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.local_scope
        self.local_scope = False
        self.generic_visit(node)
        self.local_scope = previous
        return node

    def visit_Lambda(self, node):
        return node

    def visit_Compare(self, node):
        site = location(self.path, node)
        candidate = (
            self.local_scope and len(node.ops) == 1
            and type(node.ops[0]) in OPERATOR_NAMES
            and (literal_threshold(node.left) or literal_threshold(node.comparators[0]))
        )
        self.generic_visit(node)
        if not candidate:
            return node
        operation = OPERATOR_NAMES[type(node.ops[0])]
        if not self.instrument:
            self.operators[site] = operation
            node.ops[0] = ast.Lt() if operation.startswith("l") else ast.Gt()
            return node
        self.sites.add((f"@ordered-site:{site}", 0))
        return ast.copy_location(ast.Call(
            func=ast.Name(id="_blast_radius_ordered", ctx=ast.Load()),
            args=[node.left, node.comparators[0], ast.Constant(value=operation), ast.Constant(value=site)],
            keywords=[],
        ), node)


def changed_ordered_sites(path, source, updated):
    before = ast.parse(source.encode("utf-8"), path)
    after = ast.parse(updated.encode("utf-8"), path)
    old, new = OrderedComparisons(path), OrderedComparisons(path)
    old.visit(before)
    new.visit(after)
    if ast.dump(before) != ast.dump(after) or len(old.operators) != len(new.operators):
        return set(), set()
    changed = {
        site for (site, operation), replacement in zip(old.operators.items(), new.operators.values())
        if operation != replacement
    }
    return changed, enclosing_code_lines(before, changed)


def observe_order(scope, left, right, operation, site):
    left_type, right_type = type(left), type(right)
    pure = left_type is right_type is int or (
        left_type is right_type is tuple
        and all(type(item) is int for item in left)
        and all(type(item) is int for item in right)
    )
    if not pure:
        scope.add((f"@ordered-unsafe:{site}", 0))
    elif left == right:
        scope.add((f"@ordered-boundary:{site}", 0))
    return OPERATORS[operation](left, right)
