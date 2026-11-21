"""Classify changes to required argument flags in pinned SQLGlot expressions."""

import ast


def argument_structure(module):
    """Mask literal Boolean flags and retain the original values by class."""
    flags = {}
    copies = {}
    for definition in module.body:
        if not isinstance(definition, ast.ClassDef) or definition.decorator_list:
            continue
        assignments = [
            statement.value
            for statement in definition.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "arg_types"
                for target in statement.targets
            )
        ]
        if len(assignments) != 1 or not isinstance(assignments[0], ast.Dict):
            continue
        arguments = assignments[0]
        # The pinned checkout's argument dictionary spreads use same-module classes.
        copies[definition.name] = {
            value.value.id
            for key, value in zip(arguments.keys, arguments.values)
            if key is None and isinstance(value, ast.Attribute) and value.attr == "arg_types"
            and isinstance(value.value, ast.Name)
        }
        if not all(
            isinstance(key, ast.Constant) and type(key.value) is str
            and isinstance(value, ast.Constant) and type(value.value) is bool
            for key, value in zip(arguments.keys, arguments.values)
        ):
            continue
        flags[definition.name] = tuple(value.value for value in arguments.values)
        for value in arguments.values:
            value.value = False
    return ast.dump(module), flags, copies


def changed_expression_validators(path, source, updated):
    before, original_flags, copies = argument_structure(ast.parse(source.encode("utf-8"), path))
    after, updated_flags, _ = argument_structure(ast.parse(updated.encode("utf-8"), path))
    if before != after:
        return set()

    changed = {name for name, flags in original_flags.items() if flags != updated_flags[name]}
    pending = list(changed)
    while pending:
        source_class = pending.pop()
        for name, parents in copies.items():
            if source_class in parents and name not in changed:
                changed.add(name)
                pending.append(name)
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    return {(f"@expr:{module}.{name}", 0) for name in changed}
