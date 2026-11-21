"""Recognize edits to traced dialect and tokenizer fields."""

import ast


def integer_literal(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = node.operand
        if isinstance(value, ast.Constant) and type(value.value) is int:
            return -value.value if isinstance(node.op, ast.USub) else value.value
    return None


def numeric_escapes(value):
    if not isinstance(value, ast.Dict):
        return {}
    escapes = {}
    for key, arguments in zip(value.keys, value.values):
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or key.value in escapes
            or not isinstance(arguments, ast.Tuple)
            or len(arguments.elts) != 4
        ):
            return {}
        integers = tuple(integer_literal(item) for item in arguments.elts)
        if None in integers:
            return {}
        escapes[key.value] = integers
    return escapes


def field_structure(module, module_name):
    values = {}
    classes = [(f"{module_name}.{node.name}", node) for node in module.body if isinstance(node, ast.ClassDef)]
    while classes:
        name, definition = classes.pop()
        if definition.decorator_list:
            continue
        for statement in definition.body:
            if isinstance(statement, ast.ClassDef):
                classes.append((f"{name}.{statement.name}", statement))
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
            ):
                continue
            field = statement.targets[0].id
            if field == "WEEK_OFFSET":
                value = integer_literal(statement.value)
                if value is not None:
                    values[f"@week:{name}"] = value
                    statement.value = ast.Constant(value=0)
            elif field == "NUMERIC_ESCAPES":
                escapes = numeric_escapes(statement.value)
                if escapes:
                    for key, arguments in escapes.items():
                        values[f"@numeric:{name}:{key}"] = arguments
                    # Class creation and generation inspect these keys without a lookup probe.
                    statement.value = ast.Constant(value=tuple(key for key in ("x", "0", "u") if key in escapes))
    return ast.dump(module), values


def changed_dialect_fields(path, source, updated):
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    before, original_fields = field_structure(ast.parse(source.encode("utf-8"), path), module)
    after, updated_fields = field_structure(ast.parse(updated.encode("utf-8"), path), module)
    if before != after:
        return set(), set()
    fields = {
        tag for tag in original_fields.keys() | updated_fields.keys()
        if original_fields.get(tag) != updated_fields.get(tag)
    }
    return fields, updated_fields.keys() - original_fields.keys()
