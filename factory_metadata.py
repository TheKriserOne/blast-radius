"""Discover baseline parser factories that only construct a closure."""

import __future__
import ast
import importlib
import inspect
import symtable
import sys
import textwrap
from collections import defaultdict
from pathlib import Path, PurePosixPath
from types import CodeType, FunctionType


def plain_parameters(definition, deferred_annotations):
    args = definition.args
    if (
        definition.decorator_list
        or getattr(definition, "type_params", ())
        or args.defaults
        or any(default is not None for default in args.kw_defaults)
    ):
        return False
    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    parameters.extend(arg for arg in (args.vararg, args.kwarg) if arg is not None)
    return deferred_annotations or (
        definition.returns is None and all(arg.annotation is None for arg in parameters)
    )


def closure_code(function, root):
    if type(function) is not FunctionType:
        return None
    filename = Path(function.__code__.co_filename).resolve()
    if not filename.is_relative_to(root / "sqlglot"):
        return None
    try:
        definitions = ast.parse(textwrap.dedent(inspect.getsource(function))).body
    except (OSError, SyntaxError, TypeError):
        return None

    if len(definitions) != 1 or not isinstance(definitions[0], ast.FunctionDef):
        return None
    definition = definitions[0]
    deferred = bool(function.__code__.co_flags & __future__.annotations.compiler_flag)
    body = definition.body
    if ast.get_docstring(definition, clean=False) is not None:
        body = body[1:]
    if (
        not plain_parameters(definition, deferred)
        or len(body) != 2
        or not isinstance(body[0], ast.FunctionDef)
        or not isinstance(body[1], ast.Return)
        or not isinstance(body[1].value, ast.Name)
        or body[1].value.id != body[0].name
        or not plain_parameters(body[0], deferred)
    ):
        return None

    return next(
        (
            code for code in function.__code__.co_consts
            if isinstance(code, CodeType) and code.co_name == body[0].name
        ),
        None,
    )


def class_bindings(definition, symbol_table, parser_class, module, root):
    globals_by_name = vars(module)
    results = defaultdict(list)
    for statement in definition.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Dict)
        ):
            continue
        actual = vars(parser_class).get(statement.targets[0].id)
        for key, value in zip(statement.value.keys, statement.value.values):
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
                continue
            binding = value.func.id
            code = closure_code(globals_by_name.get(binding), root)
            if code is None:
                continue
            resolved = (
                actual.get(key.value)
                if type(actual) is dict
                and isinstance(key, ast.Constant)
                and type(key.value) is str
                else None
            )
            results[binding].append(
                symbol_table.lookup(binding).is_global()
                and type(resolved) is FunctionType
                and resolved.__code__ is code
            )
    return sorted(name for name, proven in results.items() if all(proven))


def build_factory_metadata(root, module_paths):
    """Return {source path: {parser class name: [factory binding names]}}."""
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    try:
        parser_module = importlib.import_module("sqlglot.parser")
        if Path(parser_module.__file__).resolve() != root / "sqlglot/parser.py":
            raise ValueError("sqlglot.parser was loaded from outside the pinned checkout")

        metadata = {}
        for path in sorted(set(module_paths)):
            relative = PurePosixPath(path)
            if relative.parts[:2] != ("sqlglot", "parsers") or relative.suffix != ".py":
                continue
            filename = root / path
            if not filename.is_file():
                continue
            module_parts = relative.with_suffix("").parts
            if module_parts[-1] == "__init__":
                module_parts = module_parts[:-1]
            try:
                module = importlib.import_module(".".join(module_parts))
            except Exception:
                # Optional modules with unresolved dependencies get no specialization.
                continue
            module_file = getattr(module, "__file__", None)
            if module_file is None or Path(module_file).resolve() != filename.resolve():
                continue

            source = filename.read_text(encoding="utf-8")
            definitions = ast.parse(source, filename).body
            scopes = {
                scope.get_lineno(): scope
                for scope in symtable.symtable(source, str(filename), "exec").get_children()
                if scope.get_type() == "class"
            }
            classes = {}
            for definition in definitions:
                if not isinstance(definition, ast.ClassDef):
                    continue
                cls = vars(module).get(definition.name)
                scope = scopes.get(definition.lineno)
                if (
                    type(cls) is not type
                    or cls.__module__ != module.__name__
                    or cls.__qualname__ != definition.name
                    or not issubclass(cls, parser_module.Parser)
                    or any("__init_subclass__" in vars(base) for base in cls.__mro__[:-1])
                    or any(hasattr(type(value), "__set_name__") for value in vars(cls).values())
                    or definition.decorator_list
                    or definition.keywords
                    or getattr(definition, "type_params", ())
                    or scope is None
                ):
                    continue
                bindings = class_bindings(definition, scope, cls, module, root)
                if bindings:
                    classes[definition.name] = bindings
            if classes:
                metadata[path] = classes
        return metadata
    finally:
        sys.path.pop(0)
