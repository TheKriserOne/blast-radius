"""Precompute exact syntax summaries for the pinned SQLGlot sources."""

import marshal
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from impact import FunctionBodies


def build_syntax(baseline):
    with ZipFile(baseline / "syntax.zip", "w") as archive:
        for path in sorted((baseline / "sqlglot").rglob("*.py")):
            relative = path.relative_to(baseline).as_posix()
            raw = path.read_bytes()
            try:
                summary = FunctionBodies(raw.decode("utf-8"), relative)
            except (RecursionError, SyntaxError, ValueError):
                continue
            data = marshal.dumps((sha256(raw).digest(), summary.structure, summary.bodies))
            archive.writestr(ZipInfo(relative), data, compress_type=ZIP_DEFLATED, compresslevel=3)


if __name__ == "__main__":
    build_syntax(Path(sys.argv[1]))
