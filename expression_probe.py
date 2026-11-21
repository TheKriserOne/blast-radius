"""Capture expression validation receiver classes during baseline coverage."""

import sys
from functools import wraps
from importlib.machinery import PathFinder
from pathlib import Path


EXPRESSION_MODULE = "sqlglot.expressions.core"
EXPRESSION_PATH = "sqlglot/expressions/core.py"
_probe = None


class ExpressionProbe:
    def __init__(self, baseline):
        self.source = (Path(baseline) / EXPRESSION_PATH).resolve()
        self.pending = set()
        self.scope = lambda: self.pending
        self.tags_by_type = {}
        loaded = sys.modules.get(EXPRESSION_MODULE)
        self.started_before_import = loaded is None
        if loaded is not None:
            self.attach(loaded)
        sys.meta_path.insert(0, self)

    def attach(self, module):
        original = module.Expression.error_messages
        if (
            Path(original.__code__.co_filename).resolve() != self.source
            or original.__code__.co_qualname != "Expression.error_messages"
        ):
            raise RuntimeError("Unexpected SQLGlot expression validation implementation")

        probe = self

        @wraps(original)
        def track(self, args=None):
            cls = type(self)
            tags = probe.tags_by_type.get(cls)
            if tags is None:
                tags = tuple(
                    (f"@expr:{base.__module__}.{base.__qualname__}", 0)
                    for base in cls.__mro__
                    if base.__module__.startswith("sqlglot.")
                )
                probe.tags_by_type[cls] = tags
            probe.scope().update(tags)
            return original(self, args)

        module.Expression.error_messages = track

    def find_spec(self, name, path=None, target=None):
        if name != EXPRESSION_MODULE:
            return None
        spec = PathFinder.find_spec(name, path, target)
        if spec is None or spec.loader is None:
            return spec
        execute = spec.loader.exec_module

        def exec_module(module):
            execute(module)
            self.attach(module)

        spec.loader.exec_module = exec_module
        return spec


def observe_expression_validation(baseline, scope=None):
    """Start before SQLGlot loads and move startup observations into the first scope."""
    global _probe
    if _probe is None:
        _probe = ExpressionProbe(baseline)
    if scope is not None:
        scope().update(_probe.pending)
        _probe.pending.clear()
        _probe.scope = scope
    return _probe.started_before_import
