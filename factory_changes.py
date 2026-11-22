"""Recognize edits to certified factory arguments and parser table entries."""

import ast


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


def function_table_structure(module, module_name, builders):
    if builders:
        imports = [
            (node, alias) for node in ast.walk(module) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names if (alias.asname or alias.name.split(".")[0]) == "exp"
        ]
        if not (
            len(imports) == 1 and isinstance(imports[0][0], ast.ImportFrom)
            and imports[0][0] in module.body and imports[0][0].module == "sqlglot" and imports[0][0].level == 0
            and imports[0][1].name == "exp"
        ) or any(
            (isinstance(node, ast.Name) and node.id == "exp" and not isinstance(node.ctx, ast.Load))
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "exp")
            for node in ast.walk(module)
        ):
            builders = ()
    tables = {}
    for cls in module.body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list:
            continue
        for statement in cls.body:
            match statement:
                case (ast.Assign(targets=[ast.Name(id="FUNCTIONS")], value=ast.Dict() as table)
                      | ast.AnnAssign(target=ast.Name(id="FUNCTIONS"), value=ast.Dict() as table)):
                    pass
                case _:
                    continue
            owner = f"{module_name}.{cls.name}"
            if owner in tables:
                return None, {}
            fixed, pure, seen = [], {}, set()
            keys, expressions = [], []
            for key, value in zip(table.keys, table.values):
                if isinstance(key, ast.Constant) and type(key.value) is str:
                    name = key.value
                    if name in seen:
                        return None, {}
                    seen.add(name)
                    if pure_function_entry(value, builders):
                        # The position relative to a spread can decide which binding wins.
                        pure[name] = ast.dump(value), len(keys)
                        continue
                    fixed.append(name)
                    key.value = ""
                keys.append(key)
                expressions.append(value)
            tables[owner] = fixed, pure
            table.keys, table.values = keys, expressions
    return ast.dump(module), tables


def changed_function_keys(path, source, updated, builders):
    module_name = path.removesuffix(".py").replace("/", ".")
    before, original = function_table_structure(ast.parse(source.encode("utf-8"), path), module_name, builders)
    after, replacement = function_table_structure(ast.parse(updated.encode("utf-8"), path), module_name, builders)
    if before is None or before != after or original.keys() != replacement.keys():
        return {}
    changes = {}
    for owner, (fixed, pure) in original.items():
        new_fixed, new_pure = replacement[owner]
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
