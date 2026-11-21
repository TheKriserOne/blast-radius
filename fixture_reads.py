"""Distinguish raw fixture reads from the pinned SQL fixture loaders."""

def filtered_dependency(path):
    return f"@fixture-filtered:{path}", 0


def pair_dependency(path):
    return f"@fixture-pairs:{path}", 0


def fixture_dependency(path, caller, root):
    if path.endswith(".sql") and caller.co_filename == root + "tests/helpers.py":
        if caller.co_name == "load_sql_fixture_pairs":
            return pair_dependency(path)
        if caller.co_name == "load_sql_fixtures":
            return filtered_dependency(path)
    return path, 0


def filter_comments(source):
    # Match _filter_comments in the pinned tests/helpers.py.
    return "\n".join(line for line in source.splitlines() if line and not line.startswith("--"))


def fixture_pairs(filtered):
    # Match load_sql_fixture_pairs and _extract_meta after comment filtering.
    statements = filtered.split(";")
    for index in range(0, len(statements) - 1, 2):
        sql_lines = statements[index].strip().split("\n")
        metadata = {}
        first_sql_line = 0
        while sql_lines[first_sql_line].startswith("#"):
            key, value = sql_lines[first_sql_line].split(":", maxsplit=1)
            metadata[key.lstrip("#").strip()] = value.strip()
            first_sql_line += 1
        yield tuple(metadata.items()), "\n".join(sql_lines[first_sql_line:]), statements[index + 1].strip()
