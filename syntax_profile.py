"""Compare patched syntax with precomputed baseline normalizations."""

import ast
import marshal
import sys
import warnings
from collections import defaultdict
from types import CodeType


PROFILE_DIRECTORY = f"matchers-v1-{sys.implementation.cache_tag}"


def equivalent_module(path, source, updated):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        before = ast.parse(source, path, type_comments=True)
        after = ast.parse(updated, path, type_comments=True)
        if ast.dump(before, include_attributes=True) != ast.dump(after, include_attributes=True):
            return False
        before_code = compile(source, path, "exec", dont_inherit=True)
        after_code = compile(updated, path, "exec", dont_inherit=True)
        return marshal.dumps(before_code) == marshal.dumps(after_code)


def baseline_profile(path, source, normalize):
    module = ast.parse(source.encode("utf-8"), path)
    normalized, sites = normalize(module, path)
    scopes = tuple(
        (min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))]), node.end_lineno)
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
    )
    return ast.dump(normalized), sites, scopes


def match_profile(path, source, updated, normalize, baseline):
    before, original, scopes = baseline if baseline is not None else baseline_profile(path, source, normalize)
    # Unchanged paired values cannot narrow test selection.
    if not original:
        return None
    normalized, patched = normalize(ast.parse(updated.encode("utf-8"), path), path)
    if len(original) != len(patched) or all(old == new for old, new in zip(original.values(), patched.values())):
        return None
    if before != ast.dump(normalized):
        return None
    return zip(original.items(), patched.values()), scopes


def enclosing_code_lines(scopes, sites):
    lines = {int(site.rsplit(":", 2)[1]) for site in sites}
    return {1} | {start for start, end in scopes if any(start <= line <= end for line in lines)}


def nested_code_objects(module):
    pending = [value for value in module.co_consts if isinstance(value, CodeType)]
    while pending:
        code = pending.pop()
        yield code
        pending.extend(value for value in code.co_consts if isinstance(value, CodeType))


def code_at_lines(path, source, lines):
    by_line = defaultdict(list)
    module = compile(source.encode("utf-8"), path, "exec", dont_inherit=True)
    for code in nested_code_objects(module):
        if code.co_firstlineno in lines:
            by_line[code.co_firstlineno].append(marshal.dumps(code))
    return by_line
