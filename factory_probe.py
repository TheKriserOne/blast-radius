"""Track baseline invocations of certified parser-table factory results."""

import ast
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
import sys
from types import FunctionType


FACTORY_MAP = "BLAST_RADIUS_FACTORY_MAP"
SHOW_TABLE = "SHOW_PARSERS"
_probe = None


def show_entries(module, factories):
    """Find unique direct entries following all dict spread entries."""
    entries = {}
    for cls in module.body:
        if not isinstance(cls, ast.ClassDef) or cls.name not in factories:
            continue
        assignments = [
            stmt for stmt in cls.body if isinstance(stmt, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == SHOW_TABLE for target in stmt.targets)
        ]
        if len(assignments) != 1 or len(assignments[0].targets) != 1:
            continue
        table = assignments[0].value
        if not isinstance(table, ast.Dict):
            continue
        for index, (key, call) in enumerate(zip(table.keys, table.values)):
            if (
                not isinstance(key, ast.Constant)
                or type(key.value) is not str
                or not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Name)
                or call.func.id not in factories[cls.name]
            ):
                continue
            if not all(
                isinstance(later_key, ast.Constant) and later_key.value != key.value
                for later_key in table.keys[index + 1:]
            ):
                continue
            if any(
                isinstance(earlier_key, ast.Constant) and earlier_key.value == key.value
                for earlier_key in table.keys[:index]
            ):
                continue
            entries[cls.name, SHOW_TABLE, key.value] = call
    return entries


def changed_show_entries(before, after, factories):
    """Isolate edits consisting solely of same-type literal arguments at these entries."""
    old, new = deepcopy(before), deepcopy(after)
    old_calls, new_calls = show_entries(old, factories), show_entries(new, factories)
    if old_calls.keys() != new_calls.keys():
        return None
    changed = {site for site in old_calls if ast.dump(old_calls[site]) != ast.dump(new_calls[site])}
    for calls in (old_calls, new_calls):
        for site in changed:
            call = calls[site]
            for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
                if isinstance(argument, ast.Constant):
                    argument.value = type(argument.value).__name__
    return changed if changed and ast.dump(old) == ast.dump(new) else None


def tag(kind, path, site):
    return ("@factory:" + kind + ":" + json.dumps([path, *site], separators=(",", ":")), 0)


def opaque(path):
    return ("@factory:opaque:" + path, 0)


class FactoryProbe:
    def __init__(self, baseline, table_map):
        self.baseline = Path(baseline).resolve()
        self.modules = {}
        for path, factories in table_map.items():
            if not path.startswith("sqlglot/parsers/") or path.endswith("__init__.py"):
                continue
            filename = self.baseline / path
            entries = tuple(show_entries(ast.parse(filename.read_bytes(), filename), factories))
            if entries:
                self.modules[path[:-3].replace("/", ".")] = (path, entries)
        self.pending = set()
        self.scope = None
        self.by_function = {}
        self.by_code_id = {}
        sys.addaudithook(self.audit)
        for name in self.modules.keys() & sys.modules.keys():
            self.mark_opaque(self.modules[name][0])

    def mark_opaque(self, path):
        (self.pending if self.scope is None else self.scope()).add(opaque(path))

    def attach(self, module):
        source_path, entries = self.modules[module.__name__]
        filename = self.baseline / source_path
        if Path(module.__file__).resolve() != filename or self.scope is None:
            self.mark_opaque(source_path)
            return
        for site in entries:
            class_name, table, key = site
            cls = vars(module).get(class_name)
            dictionary = vars(cls).get(table) if isinstance(cls, type) else None
            function = dictionary.get(key) if type(dictionary) is dict else None
            if type(function) is not FunctionType:
                self.mark_opaque(source_path)
                continue
            record = self.by_function.get(function)
            if record is None:
                # Keep the original function and its cells. CodeType equality is
                # structural, so use identity and retain the exact code object.
                code = function.__code__.replace()
                function.__code__ = code
                record = (code, set(), set())
                self.by_function[function] = record
                self.by_code_id[id(code)] = record
            _, tags, owners = record
            tags.add(tag("called", source_path, site))
            owners.add(source_path)
            self.scope().add(tag("registered", source_path, site))

    def called(self, code, dependencies):
        record = self.by_code_id.get(id(code))
        if record is not None:
            dependencies.update(record[1])

    def audit(self, event, args):
        if event not in {"object.__getattr__", "object.__setattr__", "object.__delattr__"}:
            return
        receiver, attribute = args[:2]
        if type(receiver) is FunctionType and attribute in {"__code__", "__defaults__", "__kwdefaults__"}:
            record = self.by_function.get(receiver)
            if record is not None:
                for source_path in record[2]:
                    self.mark_opaque(source_path)


def observe_factory_calls(baseline, scope=None):
    global _probe
    if _probe is None:
        path = os.environ.get(FACTORY_MAP)
        if not path:
            return None
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            _probe = FactoryProbe(baseline, json.load(stream)["factories"])
    if scope is not None:
        scope().update(_probe.pending)
        _probe.pending.clear()
        _probe.scope = scope
    return _probe


def attach_factory_calls(module):
    if _probe is not None and module.__name__ in _probe.modules:
        _probe.attach(module)
