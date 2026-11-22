"""Capture expression validation receiver classes during baseline coverage."""

import sys
from functools import partial, wraps
from importlib.machinery import PathFinder
from pathlib import Path

from dialect_probe import DialectProbe


EXPRESSION_MODULE = "sqlglot.expressions.core"
EXPRESSION_PATH = "sqlglot/expressions/core.py"
_probe = None
PLAIN_ARGUMENT_TYPES = {bool, int, float, complex, str, bytes, tuple, dict, set, frozenset}


def execute_expression_module(execute, attach, module):
    execute(module)
    attach(module)


def class_attribute(cls, name):
    for base in cls.__mro__:
        namespace = vars(base)
        if name in namespace:
            return namespace[name]
    return None


def missing_argument(value):
    if value is None:
        return True
    cls = type(value)
    if cls is list:
        return not value
    if type(cls) is not type:
        return None
    if cls in PLAIN_ARGUMENT_TYPES:
        return False
    if any(
        base is list or "__class__" in vars(base) or "__getattribute__" in vars(base)
        for base in cls.__mro__ if base is not object
    ):
        return None
    return False


def missing_optional_arguments(expression, args_slot, dict_slot):
    cls = type(expression)
    if (
        type(cls) is not type
        or class_attribute(cls, "__getattribute__") is not object.__getattribute__
        or class_attribute(cls, "__class__") is not object.__dict__["__class__"]
        or class_attribute(cls, "args") is not args_slot
        or class_attribute(cls, "__dict__") is not dict_slot
    ):
        return None
    fields = dict_slot.__get__(expression)
    if (
        type(fields) is not dict or any(type(key) is not str for key in fields)
        or "arg_types" in fields or "required_args" in fields
    ):
        return None
    flags = class_attribute(cls, "arg_types")
    required = class_attribute(cls, "required_args")
    try:
        arguments = args_slot.__get__(expression)
    except AttributeError:
        return None
    if (
        type(arguments) is not dict or type(flags) is not dict or type(required) is not set
        or any(type(key) is not str for key in arguments)
        or any(type(key) is not str or type(flag) is not bool for key, flag in flags.items())
        or any(type(key) is not str for key in required)
        or required != {key for key, flag in flags.items() if flag}
    ):
        return None
    missing = [missing_argument(arguments.get(key)) for key in required]
    # A changed required set can reorder multiple missing-argument errors.
    if None in missing or missing.count(True) > 1:
        return None
    return {key for key, flag in flags.items() if not flag and missing_argument(arguments.get(key)) is not False}


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
        DialectProbe(baseline, lambda: self.scope())

    def attach(self, module):
        original = module.Expression.error_messages
        args_slot = module.Expression.__dict__["args"]
        dict_slot = class_attribute(module.Expression, "__dict__")
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
            dependencies = probe.scope()
            dependencies.update(tags)
            missing = missing_optional_arguments(self, args_slot, dict_slot)
            for tag, _ in tags:
                name = tag.removeprefix("@expr:")
                if missing is None:
                    dependencies.add((f"@expr-opaque:{name}", 0))
                else:
                    dependencies.update((f"@expr-missing:{name}:{key}", 0) for key in missing)
            return original(self, args)

        module.Expression.error_messages = track

    def find_spec(self, name, path=None, target=None):
        if name != EXPRESSION_MODULE:
            return None
        spec = PathFinder.find_spec(name, path, target)
        if spec is None or spec.loader is None:
            return spec
        spec.loader.exec_module = partial(execute_expression_module, spec.loader.exec_module, self.attach)
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
