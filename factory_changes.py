"""Recognize literal edits to proven closure factories in parser tables."""

import ast


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
