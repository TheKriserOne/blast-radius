"""Record dialect, tokenizer, and generator mapping reads on the pinned checkout."""

import ast
import sys
from contextvars import ContextVar
from importlib.machinery import PathFinder
from pathlib import Path
from types import BuiltinMethodType

from factory_probe import attach_factory_calls
from guard_comparisons import GuardComparisons, supports_guard_profile
from identity_comparisons import IdentityProbe, IdentityReads, supports_identity_profile
from literal_statements import LiteralStatements, supports_literal_profile
from ordered_comparisons import OrderedComparisons, observe_order, supports_ordered_profile


MODULES = {
    "sqlglot.dialects.dialect",
    "sqlglot.expressions.builders",
    "sqlglot.generator",
    "sqlglot.optimizer.simplify",
    "sqlglot.tokenizer_core",
}
TYPE_MAPPING_SITES = {"sqlglot.generator": 1, "sqlglot.expressions.builders": 2}
PROBE_SOURCE_READ = ContextVar("probe_source_read", default=None)


def mapping_classes(base, field, mapping):
    classes = [base] if base is not None else []
    seen = set()
    while classes:
        cls = classes.pop()
        if cls in seen:
            continue
        seen.add(cls)
        classes.extend(cls.__subclasses__())
        if vars(cls).get(field) is mapping:
            yield cls


class DialectReads(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module
        self.numeric_lookups = 0
        self.type_mapping_lookups = 0

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr == "WEEK_OFFSET" and isinstance(node.ctx, ast.Load):
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="_blast_radius_week_offset", ctx=ast.Load()),
                    args=[node.value],
                    keywords=[],
                ),
                node,
            )
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        getter = node.func
        if (
            isinstance(getter, ast.Attribute)
            and getter.attr == "get"
            and isinstance(getter.value, ast.Attribute)
            and getter.value.attr == "numeric_escapes"
            and len(node.args) == 1
            and not node.keywords
        ):
            self.numeric_lookups += 1
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="_blast_radius_numeric_escape", ctx=ast.Load()),
                    args=[getter.value, node.args[0]],
                    keywords=[],
                ),
                node,
            )
        if isinstance(getter, ast.Attribute) and getter.attr == "get" and len(node.args) == 2 and not node.keywords:
            table = getter.value
            if (
                (self.module == "sqlglot.generator" and isinstance(table, ast.Attribute) and table.attr == "TYPE_MAPPING")
                or (self.module == "sqlglot.expressions.builders" and isinstance(table, ast.Name) and table.id == "type_mapping")
            ):
                self.type_mapping_lookups += 1
                return ast.copy_location(ast.Call(
                    func=ast.Name(id="_blast_radius_type_mapping", ctx=ast.Load()),
                    args=[getter, *node.args],
                    keywords=[],
                ), node)
        return node


class DialectProbe:
    def __init__(self, baseline, scope):
        self.baseline = Path(baseline).resolve()
        self.scope = scope
        self.identity = IdentityProbe(baseline, scope)
        self.week_tags = {}
        self.escape_owners = {}
        self.type_map_owners = {}
        self.type_map_lookups = {}
        if any(
            module in MODULES
            or supports_identity_profile(module)
            or supports_guard_profile(module)
            or supports_literal_profile(module)
            or supports_ordered_profile(module)
            for module in sys.modules
        ):
            raise RuntimeError("Dialect profiling began after SQLGlot was loaded")
        sys.meta_path.insert(0, self)

    def week_offset(self, dialect):
        cls = dialect if isinstance(dialect, type) else type(dialect)
        tags = self.week_tags.get(cls)
        if tags is None:
            tags = tuple(
                (f"@week:{base.__module__}.{base.__qualname__}", 0)
                for base in cls.__mro__
                if base.__module__.startswith("sqlglot.")
            )
            self.week_tags[cls] = tags
        self.scope().update(tags)
        return dialect.WEEK_OFFSET

    def owners(self, mapping):
        identity = id(mapping)
        if identity not in self.escape_owners:
            tokenizer = getattr(sys.modules.get("sqlglot.tokens"), "Tokenizer", None)
            owners = tuple(
                f"{cls.__module__}.{cls.__qualname__}"
                for cls in mapping_classes(tokenizer, "NUMERIC_ESCAPES", mapping)
            )
            # Retain the dictionary so a later allocation cannot reuse its identity.
            self.escape_owners[identity] = mapping, owners
        return self.escape_owners[identity][1]

    def numeric_escape(self, mapping, key):
        value = mapping.get(key)
        dependencies = self.scope()
        owners = self.owners(mapping)
        for owner in owners:
            dependencies.add((f"@numeric-owner:{owner}", 0))
            if value is not None:
                dependencies.add((f"@numeric:{owner}:{key}", 0))
            else:
                dependencies.add((f"@numeric-miss:{owner}:{key}", 0))
        if not owners:
            kind = "@numeric-unknown" if value is not None else "@numeric-unknown-miss"
            dependencies.add((f"{kind}:{key}", 0))
        return value

    def type_mapping(self, getter, key, default):
        result = getter(key, default)
        scope = self.scope()
        dtype = getattr(sys.modules.get("sqlglot.expressions.datatypes"), "DType", None)
        if type(getter) is not BuiltinMethodType or type(getter.__self__) is not dict or type(key) is not dtype:
            scope.add(("@type-map-unknown", 0))
            return result
        mapping = getter.__self__
        identity = id(mapping)
        lookup = identity, key
        if lookup not in self.type_map_lookups:
            if identity not in self.type_map_owners:
                generator = getattr(sys.modules.get("sqlglot.generator"), "Generator", None)
                # Pinned table copies take their entries from classes in the resulting owner's MRO.
                owners = {
                    f"{base.__module__}.{base.__qualname__}"
                    for cls in mapping_classes(generator, "TYPE_MAPPING", mapping)
                    for base in cls.__mro__ if base.__module__.startswith("sqlglot.")
                }
                self.type_map_owners[identity] = mapping, owners
            _, owners = self.type_map_owners[identity]
            self.type_map_lookups[lookup] = (
                {(f"@type-map:{owner}:{key.name}", 0) for owner in owners}
                | {(f"@type-map-owner:{owner}", 0) for owner in owners}
                if owners else {("@type-map-unknown", 0)}
            )
        scope.update(self.type_map_lookups[lookup])
        return result

    def guard(self, site):
        self.scope().add((f"@guard-read:{site}", 0))

    def literal_assignment(self, site):
        self.scope().add((f"@literal-read:{site}", 0))

    def ordered(self, left, right, operation, site, side):
        return observe_order(self.scope(), left, right, operation, site, side)

    def find_spec(self, name, path=None, target=None):
        identity_profile = supports_identity_profile(name)
        guard_profile = supports_guard_profile(name)
        literal_profile = supports_literal_profile(name)
        ordered_profile = supports_ordered_profile(name)
        if not any((name in MODULES, identity_profile, guard_profile, literal_profile, ordered_profile)):
            return None
        spec = PathFinder.find_spec(name, path, target)
        if spec is None or spec.loader is None:
            return spec
        expected = self.baseline.joinpath(*name.split(".")).with_suffix(".py")
        if Path(spec.origin).resolve() != expected:
            raise RuntimeError("Dialect profiling requires the pinned Python source")
        context = PROBE_SOURCE_READ.set(str(expected))
        try:
            source = spec.loader.get_source(name)
        finally:
            PROBE_SOURCE_READ.reset(context)
        reads = DialectReads(name)
        tree = reads.visit(ast.parse(source, spec.origin))
        if name == "sqlglot.tokenizer_core" and reads.numeric_lookups != 2:
            raise RuntimeError("Expected both numeric escape readers in the pinned tokenizer")
        if name in TYPE_MAPPING_SITES and reads.type_mapping_lookups != TYPE_MAPPING_SITES[name]:
            raise RuntimeError("Expected all TYPE_MAPPING lookup sites in the pinned source")
        relative_path = expected.relative_to(self.baseline).as_posix()
        comparisons = IdentityReads(relative_path)
        if identity_profile:
            tree = comparisons.visit(tree)
        guards = GuardComparisons(relative_path, instrument=True)
        if guard_profile:
            tree = guards.visit(tree)
        literals = LiteralStatements(relative_path, instrument=True)
        if literal_profile:
            tree = literals.visit(tree)
        ordered = OrderedComparisons(relative_path, instrument=True)
        if ordered_profile:
            tree = ordered.visit(tree)
        code = compile(ast.fix_missing_locations(tree), spec.origin, "exec", dont_inherit=True)

        def execute(module):
            module.__dict__["_blast_radius_week_offset"] = self.week_offset
            module.__dict__["_blast_radius_numeric_escape"] = self.numeric_escape
            module.__dict__["_blast_radius_type_mapping"] = self.type_mapping
            module.__dict__["_blast_radius_identity"] = self.identity.compare
            module.__dict__["_blast_radius_guard"] = self.guard
            module.__dict__["_blast_radius_literal"] = self.literal_assignment
            module.__dict__["_blast_radius_ordered"] = self.ordered
            self.scope().update(comparisons.sites | guards.sites | literals.sites | ordered.sites)
            if name == "sqlglot.tokenizer_core":
                self.scope().add(("@numeric-missing-keys", 0))
            if name in TYPE_MAPPING_SITES:
                self.scope().add((f"@type-map-sites:{name}", 0))
            exec(code, module.__dict__)
            attach_factory_calls(module)

        spec.loader.exec_module = execute
        return spec
