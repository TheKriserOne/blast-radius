"""Compare identity and equality only when equality is known to be pure."""

import ast
import sys
from pathlib import Path
from threading import local
from types import FunctionType, MethodType

from syntax_profile import enclosing_code_lines, match_profile


def supports_identity_profile(module):
    return module.startswith(("sqlglot.dialects.", "sqlglot.executor."))


def literal_operand(node):
    return (
        isinstance(node, ast.Constant) and node.value is None
        or isinstance(node, ast.Attribute) and node.attr.isupper()
    )


def candidate(node, operation):
    return (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], operation)
        and (literal_operand(node.left) or literal_operand(node.comparators[0]))
    )


def location(path, node):
    return f"{path}:{node.lineno}:{node.col_offset}"


def identity_structure(module, path):
    operators = {}
    for node in ast.walk(module):
        if candidate(node, (ast.Is, ast.Eq)):
            operators[location(path, node)] = isinstance(node.ops[0], ast.Is)
            node.ops[0] = ast.Is()
    return ast.dump(module), operators


def changed_identity_sites(path, source, updated, baseline=None):
    match = match_profile(path, source, updated, identity_structure, baseline)
    if match is None:
        return set(), set()
    pairs, scopes = match
    changed = set()
    for (site, was_identity), is_identity in pairs:
        if is_identity and not was_identity:
            return set(), set()
        if was_identity and not is_identity:
            changed.add(site)
    return changed, enclosing_code_lines(scopes, changed)


class IdentityReads(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.sites = set()

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not candidate(node, ast.Is):
            return node
        site = location(self.path, node)
        self.sites.add((f"@identity-site:{site}", 0))
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="_blast_radius_identity", ctx=ast.Load()),
                args=[node.left, node.comparators[0], ast.Constant(value=site)],
                keywords=[],
            ),
            node,
        )


mro_of = type.__dict__["__mro__"].__get__
dict_of = type.__dict__["__dict__"].__get__
MISSING = object()
UNSAFE = object()


def class_member(cls, attribute):
    result = MISSING
    for key, value in dict_of(cls).items():
        if type(key) is not str:
            return UNSAFE
        if key == attribute:
            result = value
    return result


def sqlglot_type(cls):
    module = class_member(cls, "__module__")
    return type(module) is str and module.startswith("sqlglot.")


def equality_slot(cls):
    for base in mro_of(cls):
        slot = class_member(base, "__eq__")
        if slot is not MISSING:
            return slot
    return None


def pure_equivalent(left, right, identity):
    if left is None and right is None:
        return True
    if left is None or right is None:
        other_type = type(right if left is None else left)
        return (
            (other_type is object or sqlglot_type(other_type))
            and equality_slot(other_type) is object.__eq__
        )
    left_type, right_type = type(left), type(right)
    if (
        (left_type is str or sqlglot_type(left_type))
        and (right_type is str or sqlglot_type(right_type))
        and any(base is str for base in mro_of(left_type))
        and any(base is str for base in mro_of(right_type))
        and equality_slot(left_type) is str.__eq__
        and equality_slot(right_type) is str.__eq__
    ):
        return str.__eq__(left, right) is identity
    return False


class IdentityProbe:
    def __init__(self, baseline, scope):
        self.prefix = str(Path(baseline).resolve()) + "/"
        self.scope = scope
        self.audit_state = local()
        sys.addaudithook(self.audit)

    def compare(self, left, right, site):
        identity = left is right
        if not pure_equivalent(left, right, identity):
            self.scope().add((f"@identity-unsafe:{site}", 0))
        return identity

    def audit(self, event, args):
        if event != "object.__getattr__" or args[1] != "__code__":
            return
        if getattr(self.audit_state, "inspecting", False):
            return
        function = args[0]
        if type(function) is not FunctionType and type(function) is not MethodType:
            return
        self.audit_state.inspecting = True
        try:
            code = function.__code__
        finally:
            self.audit_state.inspecting = False
        filename = code.co_filename
        if filename.startswith(self.prefix):
            path = filename.removeprefix(self.prefix)
            if (
                path in {"sqlglot/parser.py", "sqlglot/generator.py"}
                or path.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/optimizer/"))
                or supports_identity_profile(path.replace("/", "."))
            ):
                self.scope().add((f"@identity-code:{path}:{code.co_firstlineno}", 0))
