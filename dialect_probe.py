"""Record dialect, tokenizer, generator, and parser mapping reads on the pinned checkout."""

import ast
import sys
from collections import Counter
from contextvars import ContextVar
from importlib.machinery import PathFinder
from pathlib import Path
from types import BuiltinMethodType, MethodType

from factory_probe import ALTER_TABLE, FUNCTION_TABLES, MATCHED_TABLE_SITES, attach_factory_calls
from guard_comparisons import GuardComparisons, supports_guard_profile
from identity_comparisons import IdentityProbe, IdentityReads, supports_identity_profile
from literal_statements import LiteralStatements, supports_literal_profile
from ordered_comparisons import OrderedComparisons, observe_order, supports_ordered_profile


MODULES = {
    "sqlglot.anonymize",
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


class MappingLookups:
    def __init__(self, tag, module, base, field):
        self.tag = tag
        self.module = module
        self.base = base
        self.field = field
        self.owners = {}
        self.lookups = {}

    def dependencies(self, mapping, key):
        identity = id(mapping)
        lookup = identity, key
        if lookup not in self.lookups:
            if identity not in self.owners:
                base = getattr(sys.modules.get(self.module), self.base, None)
                # Pinned table copies take their entries from classes in the resulting owner's MRO.
                owners = {
                    f"{parent.__module__}.{parent.__qualname__}"
                    for cls in mapping_classes(base, self.field, mapping)
                    for parent in cls.__mro__ if parent.__module__.startswith("sqlglot.")
                }
                self.owners[identity] = mapping, owners
            _, owners = self.owners[identity]
            name = key if type(key) is str else key.name
            self.lookups[lookup] = (
                {(f"{self.tag}:{owner}:{name}", 0) for owner in owners}
                | {(f"{self.tag}-owner:{owner}", 0) for owner in owners}
            )
        return self.lookups[lookup]


class DialectReads(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module
        self.numeric_lookups = 0
        self.type_mapping_lookups = 0
        self.function_table_lookups = Counter()

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
            self.module == "sqlglot.parser" and isinstance(getter, ast.Attribute) and getter.attr == "_match_texts"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr in MATCHED_TABLE_SITES
        ):
            return self.function_table_read(node, node.args[0].attr, "_blast_radius_table_match", [getter, *node.args])
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
        if not isinstance(getter, ast.Attribute) or getter.attr != "get" or node.keywords:
            return node
        if len(node.args) == 2:
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
        if len(node.args) == 1:
            table = getter.value
            if (
                self.module == "sqlglot.parser" and isinstance(table, ast.Attribute)
                and table.attr in {"FUNCTION_PARSERS", ALTER_TABLE}
            ):
                return self.function_table_read(node, table.attr, "_blast_radius_function_get", [getter, *node.args])
            if (
                (self.module == "sqlglot.parser" and isinstance(table, ast.Name) and table.id == "functions")
                or (self.module in {"sqlglot.expressions.builders", "sqlglot.parsers.prql"}
                    and isinstance(table, ast.Attribute) and table.attr == "FUNCTIONS")
            ):
                return self.function_table_read(node, "FUNCTIONS", "_blast_radius_function_get", [getter, *node.args])
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.module == "sqlglot.anonymize" and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            table = node.comparators[0]
            if isinstance(table, ast.Attribute) and table.attr in FUNCTION_TABLES:
                return self.function_table_read(node, table.attr, "_blast_radius_function_contains", [node.left, table])
        return node

    def visit_Subscript(self, node):
        self.generic_visit(node)
        table = node.value
        if (
            self.module == "sqlglot.generators.snowflake" and isinstance(node.ctx, ast.Load)
            and isinstance(table, ast.Attribute) and table.attr == "FUNCTIONS"
            and isinstance(node.slice, ast.Constant)
        ):
            return self.function_table_read(node, table.attr, "_blast_radius_function_item", [table, node.slice])
        return node

    def function_table_read(self, node, field, helper, arguments):
        self.function_table_lookups[field] += 1
        return ast.copy_location(ast.Call(
            func=ast.Name(id=helper, ctx=ast.Load()),
            args=[ast.Constant(value=field), *arguments],
            keywords=node.keywords if isinstance(node, ast.Call) else [],
        ), node)


class DialectProbe:
    def __init__(self, baseline, scope):
        self.baseline = Path(baseline).resolve()
        self.scope = scope
        self.identity = IdentityProbe(baseline, scope)
        self.week_tags = {}
        self.escape_owners = {}
        self.type_maps = MappingLookups("@type-map", "sqlglot.generator", "Generator", "TYPE_MAPPING")
        self.original_match_texts = None
        self.function_tables = {
            field: MappingLookups(tag, "sqlglot.parser", "Parser", field)
            for field, (tag, _) in FUNCTION_TABLES.items()
        }
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
        scope.update(self.type_maps.dependencies(getter.__self__, key) or (("@type-map-unknown", 0),))
        return result

    def record_function_key(self, field, table, key):
        scope = self.scope()
        lookups = self.function_tables[field]
        if type(table) is dict and type(key) is str:
            scope.update(lookups.dependencies(table, key) or ((f"{lookups.tag}-unknown:{key}", 0),))
        else:
            scope.add((f"{lookups.tag}-unknown", 0))

    def function_get(self, field, getter, key):
        table = getter.__self__ if type(getter) is BuiltinMethodType else None
        self.record_function_key(field, table, key)
        return getter(key)

    def function_contains(self, field, key, table):
        self.record_function_key(field, table, key)
        return key in table

    def function_item(self, field, table, key):
        self.record_function_key(field, table, key)
        return table[key]

    def table_match(self, field, match, table, **options):
        if type(match) is MethodType and match.__func__ is self.original_match_texts:
            parser = match.__self__
            token = parser._curr
            if token.token_type not in parser.TEXT_MATCH_EXCLUDED_TOKENS:
                self.record_function_key(field, table, token.text.upper())
        else:
            self.scope().add((f"{self.function_tables[field].tag}-unknown", 0))
        return match(table, **options)

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
        function_sites = {field: tag for field, (tag, modules) in FUNCTION_TABLES.items() if name in modules}
        # ALTER SESSION reads its handler directly instead of matching the next token.
        expected_function_sites = {
            field: MATCHED_TABLE_SITES.get(field, 1) + (field == ALTER_TABLE) for field in function_sites
        }
        if reads.function_table_lookups != expected_function_sites:
            raise RuntimeError("Expected all function table lookups in the pinned source module")
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
            module.__dict__["_blast_radius_function_get"] = self.function_get
            module.__dict__["_blast_radius_function_contains"] = self.function_contains
            module.__dict__["_blast_radius_function_item"] = self.function_item
            module.__dict__["_blast_radius_table_match"] = self.table_match
            module.__dict__["_blast_radius_identity"] = self.identity.compare
            module.__dict__["_blast_radius_guard"] = self.guard
            module.__dict__["_blast_radius_literal"] = self.literal_assignment
            module.__dict__["_blast_radius_ordered"] = self.ordered
            self.scope().update(comparisons.sites | guards.sites | literals.sites | ordered.sites)
            if name == "sqlglot.tokenizer_core":
                self.scope().add(("@numeric-missing-keys", 0))
            if name in TYPE_MAPPING_SITES:
                self.scope().add((f"@type-map-sites:{name}", 0))
            self.scope().update((f"{tag}-sites:{name}", 0) for tag in function_sites.values())
            exec(code, module.__dict__)
            if name == "sqlglot.parser":
                self.original_match_texts = module.Parser._match_texts
            attach_factory_calls(module)

        spec.loader.exec_module = execute
        return spec
