"""Compare patched syntax with precomputed baseline normalizations."""

import ast
import marshal
import sys
from collections import defaultdict
from types import CodeType


PROFILE_DIRECTORY = f"matchers-v1-{sys.implementation.cache_tag}"


def baseline_profile(path, source, normalize):
    module = ast.parse(source.encode("utf-8"), path)
    shape, sites = normalize(module, path)
    scopes = tuple(
        (min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))]), node.end_lineno)
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
    )
    return shape, sites, scopes


def match_profile(path, source, updated, normalize, baseline):
    before, original, scopes = baseline if baseline is not None else baseline_profile(path, source, normalize)
    after, patched = normalize(ast.parse(updated.encode("utf-8"), path), path)
    if before != after or len(original) != len(patched):
        return None
    return zip(original.items(), patched.values()), scopes


def enclosing_code_lines(scopes, sites):
    lines = {int(site.rsplit(":", 2)[1]) for site in sites}
    return {1} | {start for start, end in scopes if any(start <= line <= end for line in lines)}


def code_at_lines(path, source, lines):
    by_line = defaultdict(list)
    module = compile(source.encode("utf-8"), path, "exec", dont_inherit=True)
    pending = [value for value in module.co_consts if isinstance(value, CodeType)]
    while pending:
        code = pending.pop()
        if code.co_firstlineno in lines:
            by_line[code.co_firstlineno].append(marshal.dumps(code))
        pending.extend(value for value in code.co_consts if isinstance(value, CodeType))
    return by_line
