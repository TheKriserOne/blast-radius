"""Distinguish raw fixture reads from the pinned SQL fixture loaders."""

SQL_LOADERS = {"load_sql_fixtures", "load_sql_fixture_pairs"}


def filtered_dependency(path):
    return f"@fixture-filtered:{path}", 0


def fixture_dependency(path, caller, root):
    if (
        path.endswith(".sql")
        and caller.co_filename == root + "tests/helpers.py"
        and caller.co_name in SQL_LOADERS
    ):
        return filtered_dependency(path)
    return path, 0


def filter_comments(source):
    # Match _filter_comments in the pinned tests/helpers.py.
    return "\n".join(line for line in source.splitlines() if line and not line.startswith("--"))
