"""Record week-offset reads and numeric-escape lookups on the pinned checkout."""

import ast
import sys
from importlib.machinery import PathFinder
from pathlib import Path


MODULES = {
    "sqlglot.dialects.dialect",
    "sqlglot.generator",
    "sqlglot.optimizer.simplify",
    "sqlglot.tokenizer_core",
}


class DialectReads(ast.NodeTransformer):
    def __init__(self):
        self.numeric_lookups = 0

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
        return node


class DialectProbe:
    def __init__(self, baseline, scope):
        self.baseline = Path(baseline).resolve()
        self.scope = scope
        self.week_tags = {}
        self.escape_owners = {}
        if MODULES & sys.modules.keys():
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
            classes = [tokenizer] if tokenizer is not None else []
            seen = set()
            owners = []
            while classes:
                cls = classes.pop()
                if cls in seen:
                    continue
                seen.add(cls)
                classes.extend(cls.__subclasses__())
                if vars(cls).get("NUMERIC_ESCAPES") is mapping:
                    owners.append(f"{cls.__module__}.{cls.__qualname__}")
            # Retain the dictionary so a later allocation cannot reuse its identity.
            self.escape_owners[identity] = mapping, tuple(owners)
        return self.escape_owners[identity][1]

    def numeric_escape(self, mapping, key):
        value = mapping.get(key)
        dependencies = self.scope()
        owners = self.owners(mapping)
        for owner in owners:
            dependencies.add((f"@numeric-owner:{owner}", 0))
            if value is not None:
                dependencies.add((f"@numeric:{owner}:{key}", 0))
        if not owners and value is not None:
            dependencies.add((f"@numeric-unknown:{key}", 0))
        return value

    def find_spec(self, name, path=None, target=None):
        if name not in MODULES:
            return None
        spec = PathFinder.find_spec(name, path, target)
        if spec is None or spec.loader is None:
            return spec
        expected = self.baseline.joinpath(*name.split(".")).with_suffix(".py")
        if Path(spec.origin).resolve() != expected:
            raise RuntimeError("Dialect profiling requires the pinned Python source")
        source = spec.loader.get_source(name)
        reads = DialectReads()
        tree = reads.visit(ast.parse(source, spec.origin))
        if name == "sqlglot.tokenizer_core" and reads.numeric_lookups != 2:
            raise RuntimeError("Expected both numeric escape readers in the pinned tokenizer")
        code = compile(ast.fix_missing_locations(tree), spec.origin, "exec", dont_inherit=True)

        def execute(module):
            module.__dict__["_blast_radius_week_offset"] = self.week_offset
            module.__dict__["_blast_radius_numeric_escape"] = self.numeric_escape
            exec(code, module.__dict__)

        spec.loader.exec_module = execute
        return spec
