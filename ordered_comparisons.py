"""Scope ordered comparison edits to observed integer operands."""

import ast
import operator

from identity_comparisons import location
from syntax_profile import enclosing_code_lines, match_profile


OPERATORS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
OPERATOR_NAMES = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}


def supports_ordered_profile(module):
    return module.startswith(("sqlglot.dialects.", "sqlglot.generators.", "sqlglot.parsers."))


def integer_literal(node):
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return sign * node.value


def literal_threshold(node):
    if isinstance(node, ast.Tuple):
        values = tuple(map(integer_literal, node.elts))
        return values if None not in values else None
    return integer_literal(node)


class OrderedComparisons(ast.NodeTransformer):
    def __init__(self, path, instrument=False):
        self.path = path
        self.instrument = instrument
        self.local_scope = False
        self.comparisons = {}
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
        if not self.local_scope or len(node.ops) != 1 or type(node.ops[0]) not in OPERATOR_NAMES:
            return self.generic_visit(node)
        left = literal_threshold(node.left)
        right = literal_threshold(node.comparators[0])
        self.generic_visit(node)
        if left is None and right is None:
            return node
        site = location(self.path, node)
        operation = OPERATOR_NAMES[type(node.ops[0])]
        side, threshold = None, None
        if left is None:
            side, threshold = 1, right
        elif right is None:
            side, threshold = 0, left
        if not self.instrument:
            self.comparisons[site] = operation, side, threshold
            node.ops[0] = ast.Lt() if operation.startswith("l") else ast.Gt()
            if side is not None:
                placeholder = ast.Constant(value=() if type(threshold) is tuple else 0)
                if side == 0:
                    node.left = placeholder
                else:
                    node.comparators[0] = placeholder
            return node
        self.sites.add((f"@ordered-site:{site}", node.end_lineno))
        if side is not None:
            self.sites.add((f"@ordered-value-site:{site}", 0))
        return ast.copy_location(ast.Call(
            func=ast.Name(id="_blast_radius_ordered", ctx=ast.Load()),
            args=[
                node.left, node.comparators[0], ast.Constant(value=operation),
                ast.Constant(value=site), ast.Constant(value=side),
            ],
            keywords=[],
        ), node)


def ordered_structure(module, path):
    normalizer = OrderedComparisons(path)
    return ast.dump(normalizer.visit(module)), normalizer.comparisons


def changed_ordered_sites(path, source, updated, baseline=None):
    match = match_profile(path, source, updated, ordered_structure, baseline)
    if match is None:
        return {}, set()
    pairs, scopes = match
    changed = {}
    for (site, original), replacement in pairs:
        if original[1] != replacement[1]:
            return {}, set()
        if original != replacement:
            changed[site] = original, replacement
    return changed, enclosing_code_lines(scopes, changed)


def threshold_changes_result(token, original, replacement):
    old_op, side, old_threshold = original
    new_op, _, new_threshold = replacement
    if type(old_threshold) is tuple:
        value = tuple(int(item, 16) for item in token.split(",")) if token else ()
    else:
        value = int(token, 16)
    if side == 0:
        return OPERATORS[old_op](old_threshold, value) != OPERATORS[new_op](new_threshold, value)
    return OPERATORS[old_op](value, old_threshold) != OPERATORS[new_op](value, new_threshold)


def observe_order(scope, left, right, operation, site, side=None):
    left_type, right_type = type(left), type(right)
    pure = left_type is right_type is int or (
        left_type is right_type is tuple
        and all(type(item) is int for item in left)
        and all(type(item) is int for item in right)
    )
    if not pure:
        scope.add((f"@ordered-unsafe:{site}", 0))
    else:
        if left == right:
            scope.add((f"@ordered-boundary:{site}", 0))
        if side is not None:
            value = right if side == 0 else left
            token = hex(value) if type(value) is int else ",".join(map(hex, value))
            scope.add((f"@ordered-value:{site}:{token}", 0))
    return OPERATORS[operation](left, right)
