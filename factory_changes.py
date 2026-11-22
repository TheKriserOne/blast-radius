"""Recognize edits to certified factory arguments and parser table entries."""

import ast

from factory_probe import FUNCTION_TABLES, MATCHED_TABLE_SITES, STATEMENT_TABLE


FUNCTION_BUILDERS_SOURCE = "sqlglot/expressions/__init__.py"


def pure_function_entry(value, builders):
    if isinstance(value, ast.Lambda):
        return (
            not value.args.defaults and all(default is None for default in value.args.kw_defaults)
            and not any(isinstance(node, ast.Name) and node.id in {"__class__", "super"} for node in ast.walk(value))
        )
    return (
        isinstance(value, ast.Attribute) and value.attr == "from_arg_list"
        and isinstance(value.value, ast.Attribute) and value.value.attr in builders
        and isinstance(value.value.value, ast.Name) and value.value.value.id == "exp"
    )


def has_module_binding(module, local_name, source, members):
    imports = [
        (node, alias) for node in ast.walk(module) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names if (alias.asname or alias.name.split(".")[0]) == local_name
    ]
    return (
        len(imports) == 1 and isinstance(imports[0][0], ast.ImportFrom)
        and imports[0][0] in module.body and imports[0][0].module == source and imports[0][0].level == 0
        and imports[0][1].name in members
        and not any(
            (isinstance(node, ast.Name) and node.id == local_name and not isinstance(node.ctx, ast.Load))
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == local_name)
            for node in ast.walk(module)
        )
    )


def argument_index_shape(value, builders):
    if not isinstance(value, ast.Lambda) or not isinstance(value.body, ast.Call):
        return None
    signature, call = value.args, value.body
    args = [*signature.posonlyargs, *signature.args]
    if len(args) != 1 or signature.vararg or signature.kwarg or signature.kwonlyargs or call.args:
        return None
    constructor = call.func
    if not (
        isinstance(constructor, ast.Attribute) and isinstance(constructor.value, ast.Name)
        and constructor.value.id == "exp" and builders.get(constructor.attr)
    ):
        return None
    keywords = []
    for keyword in call.keywords:
        match keyword:
            case ast.keyword(arg=str() as name, value=ast.Call(
                func=ast.Name(id="seq_get"), args=[ast.Name(id=arg), ast.Constant(value=index)], keywords=[],
            )) if arg == args[0].arg and type(index) is int:
                keywords.append(name)
            case _:
                return None
    return constructor.attr, ast.dump(signature), tuple(keywords)


def function_table_structure(module, module_name, builders, token_members=()):
    if builders and not has_module_binding(module, "exp", "sqlglot", {"exp", "expressions"}):
        builders = {}
    has_seq_get = builders and has_module_binding(module, "seq_get", "sqlglot.helper", {"seq_get"})
    if token_members and not has_module_binding(module, "TokenType", "sqlglot.tokens", {"TokenType"}):
        token_members = ()
    tables = {}
    for cls in module.body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list:
            continue
        for statement in cls.body:
            match statement:
                case (ast.Assign(targets=[ast.Name(id=field)], value=ast.Dict() as table)
                      | ast.AnnAssign(target=ast.Name(id=field), value=ast.Dict() as table)) if field in FUNCTION_TABLES:
                    pass
                case _:
                    continue
            statement_table = field == STATEMENT_TABLE
            if statement_table and not token_members:
                continue
            owner = field, f"{module_name}.{cls.name}"
            if owner in tables:
                return None, {}
            # These handlers receive the parser as their positional argument.
            entry_builders = {} if statement_table or field in MATCHED_TABLE_SITES else builders
            fixed, pure, seen = [], {}, set()
            keys, expressions = [], []
            for key, value in zip(table.keys, table.values):
                match key:
                    case ast.Attribute(value=ast.Name(id="TokenType"), attr=name) if (
                        statement_table and name in token_members
                    ):
                        pass
                    case ast.Constant(value=name) if not statement_table and type(name) is str:
                        pass
                    case _:
                        name = None
                if name is not None:
                    if name in seen:
                        return None, {}
                    seen.add(name)
                    if pure_function_entry(value, entry_builders):
                        # The position relative to a spread can decide which binding wins.
                        index_shape = (
                            argument_index_shape(value, entry_builders)
                            if has_seq_get and entry_builders else None
                        )
                        pure[name] = ast.dump(value), len(keys), isinstance(value, ast.Lambda), index_shape
                        continue
                    fixed.append(name)
                    if statement_table:
                        key.attr = ""
                    else:
                        key.value = ""
                keys.append(key)
                expressions.append(value)
            tables[owner] = fixed, pure
            table.keys, table.values = keys, expressions
    return ast.dump(module), tables


def changed_function_keys(path, source, updated, builders, token_members=()):
    module_name = path.removesuffix(".py").replace("/", ".")
    before, original = function_table_structure(
        ast.parse(source.encode("utf-8"), path), module_name, builders, token_members,
    )
    after, replacement = function_table_structure(
        ast.parse(updated.encode("utf-8"), path), module_name, builders, token_members,
    )
    if before is None or before != after or original.keys() != replacement.keys():
        return {}
    changes = {}
    for owner, (fixed, pure) in original.items():
        new_fixed, new_pure = replacement[owner]
        existing_builders = {value[0] for value in pure.values()}
        for key, (builder, _, is_lambda, index_shape) in new_pure.items():
            # New lambda bodies can introduce effects outside the scope of a table edit.
            if (is_lambda or key in pure) and builder not in existing_builders:
                if index_shape is None or key not in pure or index_shape != pure[key][3]:
                    return {}
        keys = {key for old, new in zip(fixed, new_fixed) if old != new for key in (old, new)}
        keys.update(key for key in pure.keys() | new_pure.keys() if pure.get(key) != new_pure.get(key))
        if keys:
            changes[owner] = keys
    return changes


def factory_argument_structure(module, factories):
    """Mask approved factory arguments in a newly parsed module with their types."""
    if not module.body or not isinstance(module.body[-1], ast.ClassDef):
        return None
    if any(
        not isinstance(statement, (ast.Import, ast.ImportFrom, ast.FunctionDef))
        or (isinstance(statement, ast.FunctionDef) and statement.decorator_list)
        for statement in module.body[:-1]
    ):
        return None

    parser = module.body[-1]
    bindings = set(factories.get(parser.name, ()))
    if not bindings or parser.decorator_list or parser.keywords:
        return None

    assignments = []
    reads = set()
    for statement in parser.body:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return None
            target = statement.targets[0].id
            bindings.discard(target)
            assignments.append((target, statement.value))
            key_iterators = {
                id(node.iter) for node in ast.walk(statement.value)
                if isinstance(node, ast.comprehension) and isinstance(node.iter, ast.Name)
            }
            reads.update(
                node.id for node in ast.walk(statement.value)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and id(node) not in key_iterators
            )
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.discard(statement.name)
            # Default values and decorators are evaluated during class creation.
            signature = [statement.args, *statement.decorator_list]
            signature.extend(getattr(statement, "type_params", ()))
            if statement.returns is not None:
                signature.append(statement.returns)
            for part in signature:
                reads.update(
                    node.id for node in ast.walk(part)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                )
        else:
            return None

    if reads & {"exec", "eval", "locals", "globals", "vars", "getattr"}:
        return None
    normalized = False
    for target, value in assignments:
        if target.startswith("__") or target in reads or not isinstance(value, ast.Dict):
            continue
        for call in value.values:
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Name)
                or call.func.id not in bindings
            ):
                continue
            for argument in [*call.args, *(kw.value for kw in call.keywords)]:
                if isinstance(argument, ast.Constant):
                    argument.value = type(argument.value).__name__
                    normalized = True
    return ast.dump(module) if normalized else None
