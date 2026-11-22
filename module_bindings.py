"""Find callers of audited module-level constants in the pinned checkout."""

import ast
import dis
import marshal
import warnings
from hashlib import sha256

from syntax_profile import nested_code_objects


# Whole-file hashes bind these audits of initializer purity and module namespace reads
# to the exact pinned files. Runtime readers are determined from their compiled code.
AUDITED_BINDINGS = {
    "sqlglot/parsers/bigquery.py": (
        "a232f6536c2cb19883a36e3354c2bc2081c5013a8bcf43e247c3a716d7c35640", "_DOMAIN_DOT", None,
    ),
    "sqlglot/tokenizer_core.py": (
        "142463b972f35299140172de19eb72bf0c32742867eb251e06c5592413f8083c", "_OCTAL_CHARS", "frozenset",
    ),
}


def binding_structure(source, path, name, initializer):
    module = ast.parse(source, path)
    literals = []
    for statement in module.body:
        match statement:
            case (ast.Assign(targets=[ast.Name(id=target)], value=value)
                  | ast.AnnAssign(target=ast.Name(id=target), value=value)) if target == name:
                if initializer is not None:
                    match value:
                        case ast.Call(func=ast.Name(id=callee), args=[argument], keywords=[]) if callee == initializer:
                            value = argument
                        case _:
                            return None
                if not isinstance(value, ast.Constant) or type(value.value) is not str:
                    return None
                literals.append(value)
    if len(literals) != 1:
        return None
    literals[0].value = ""
    return ast.dump(module)


def binding_read_lines(path, source, updated):
    spec = AUDITED_BINDINGS.get(path)
    if spec is None:
        return None
    digest, name, initializer = spec
    original, patched = source.encode("utf-8"), updated.encode("utf-8")
    if sha256(original).hexdigest() != digest:
        return None
    before = binding_structure(original, path, name, initializer)
    if before is None:
        return None
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        if before != binding_structure(patched, path, name, initializer):
            return None
        replacement = compile(patched, path, "exec", dont_inherit=True)
    if emitted:
        return None
    baseline = compile(original, path, "exec", dont_inherit=True)
    runtime_code = list(nested_code_objects(baseline))
    patched_code = list(nested_code_objects(replacement))
    if list(map(marshal.dumps, runtime_code)) != list(map(marshal.dumps, patched_code)):
        return None
    readers = {
        code.co_firstlineno
        for code in runtime_code
        if name in code.co_names and any(
            inst.opname == "LOAD_GLOBAL" and inst.argval == name for inst in dis.get_instructions(code)
        )
    }
    return readers or None
