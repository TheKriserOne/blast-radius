"""Recognize copy flag edits at audited calls on fresh SQLGlot tables."""

import re
from hashlib import sha256


FRESH_ALIAS_SOURCE = "sqlglot/optimizer/eliminate_subqueries.py"
_SOURCE_DIGEST = "bc89ed266b373257a693d1548c7d0d8304830e9d26517002623d7d29fbb467d2"
_BUILDER_DIGESTS = {
    "sqlglot/__init__.py": "ab01e0e88e2edc87a1960666366fa433f976a0c021270cf412dba32b918914d5",
    "sqlglot/helper.py": "7cdc7d042c65a996bc8867c2e370503b533a1216f9656619ec6fcffd4774f6fb",
    "sqlglot/expressions/__init__.py": "8a8c0e212718e0f53d004acad86827b4fab97539fe7ae1ada5c4c855ef7f57e0",
    "sqlglot/expressions/builders.py": "74d4e12f420a4e105933a0bcb87626b7d4fb9c68d2a3b2164a6bfefc0eb6a1e0",
    "sqlglot/expressions/core.py": "9dd10dc5c3015536c94fb99634cd412a66c103273cdd31bbc1a2482ccbf83604",
    "sqlglot/expressions/query.py": "df6bda48f09bf59c10167eb024a7d454d6a2b4d38b464da76d49c21ff296db1b",
}
_FRESH_ALIAS = re.compile(
    r"(exp\.alias_\(exp\.table_\(name\), alias=(?:to_replace\.alias or name|table\.alias_or_name))"
    r"(?:,[ \t]*copy[ \t]*=[ \t]*(?:False|True))?(\))"
)


def equivalent_fresh_alias(baseline, path, source, updated):
    if path != FRESH_ALIAS_SOURCE or source == updated or sha256(source.encode("utf-8")).hexdigest() != _SOURCE_DIGEST:
        return False

    # The pinned callers pass a detached table with a fresh identifier. Copying
    # it preserves its contents and parent links before the alias is assigned.
    # Compare the remaining source exactly to exclude all other edits.
    before, old_sites = _FRESH_ALIAS.subn(r"\1\2", source)
    after, new_sites = _FRESH_ALIAS.subn(r"\1\2", updated)
    return old_sites == new_sites == 2 and before == after and all(
        sha256((baseline / file).read_bytes()).hexdigest() == digest for file, digest in _BUILDER_DIGESTS.items()
    )
