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
        flags[definition.name] = {key.value: value.value for key, value in zip(arguments.keys, arguments.values)}
        for value in arguments.values:
            value.value = False
    return ast.dump(module), flags, copies


def with_copied_arguments(classes, copies):
    classes = set(classes)
    pending = list(classes)
    while pending:
        source_class = pending.pop()
        for name, parents in copies.items():
            if source_class in parents and name not in classes:
                classes.add(name)
                pending.append(name)
    return classes


def expression_module(path):
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def possible_argument_classes(baseline):
    """Find the classes a required-argument edit can affect."""
    classes = set()
    for path in (baseline / "sqlglot/expressions").rglob("*.py"):
        relative = path.relative_to(baseline).as_posix()
        _, flags, copies = argument_structure(ast.parse(path.read_bytes(), relative))
        module = expression_module(relative)
        classes.update(f"{module}.{name}" for name in with_copied_arguments(flags, copies))
    return classes


def changed_expression_validators(path, source, updated):
    before, original_flags, copies = argument_structure(ast.parse(source.encode("utf-8"), path))
    after, updated_flags, _ = argument_structure(ast.parse(updated.encode("utf-8"), path))
    if before != after or original_flags.keys() != updated_flags.keys():
        return set(), set()

    changed = {name for name, flags in original_flags.items() if flags != updated_flags[name]}
    flips = [
        (key, old)
        for name in changed
        for key, old in original_flags[name].items()
        if old != updated_flags[name][key]
    ]
    module = expression_module(path)
    additions = {key for key, _ in flips} if all(not old for _, old in flips) else set()
    return {(f"@expr:{module}.{name}", 0) for name in with_copied_arguments(changed, copies)}, additions
