"""Precompute exact syntax summaries for the pinned SQLGlot sources."""

import marshal
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from dialect_fields import TYPE_MEMBERS_SOURCE
from guard_comparisons import guard_structure, supports_guard_profile
from identity_comparisons import identity_structure, supports_identity_profile
from impact import FunctionBodies
from literal_statements import literal_structure, supports_literal_profile
from ordered_comparisons import ordered_structure, supports_ordered_profile
from syntax_profile import PROFILE_DIRECTORY, baseline_profile


MATCHERS = (
    ("identity", supports_identity_profile, identity_structure),
    ("guard", supports_guard_profile, guard_structure),
    ("ordered", supports_ordered_profile, ordered_structure),
    ("literal", supports_literal_profile, literal_structure),
)


def build_syntax(baseline, type_members=()):
    with ZipFile(baseline / "syntax.zip", "w") as archive:
        if type_members:
            raw = (baseline / TYPE_MEMBERS_SOURCE).read_bytes()
            data = marshal.dumps((sha256(raw).digest(), type_members))
            archive.writestr(ZipInfo(f"{PROFILE_DIRECTORY}/type-members/{TYPE_MEMBERS_SOURCE}"), data)
        for path in sorted((baseline / "sqlglot").rglob("*.py")):
            relative = path.relative_to(baseline).as_posix()
            raw = path.read_bytes()
            try:
                source = raw.decode("utf-8")
                summary = FunctionBodies(source, relative)
            except (RecursionError, SyntaxError, ValueError):
                continue
            digest = sha256(raw).digest()
            data = marshal.dumps((digest, summary.structure, summary.bodies))
            archive.writestr(ZipInfo(relative), data, compress_type=ZIP_DEFLATED, compresslevel=3)
            module = relative.removesuffix(".py").replace("/", ".")
            for kind, supports_profile, normalize in MATCHERS:
                if not supports_profile(module):
                    continue
                try:
                    profile = baseline_profile(relative, source, normalize)
                except (RecursionError, SyntaxError, ValueError):
                    continue
                data = marshal.dumps((digest, profile))
                archive.writestr(ZipInfo(f"{PROFILE_DIRECTORY}/{kind}/{relative}"), data,
                                 compress_type=ZIP_DEFLATED, compresslevel=3)


if __name__ == "__main__":
    baseline = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(baseline))
    from sqlglot.expressions import DType

    members = tuple(sorted(name for name, member in DType.__members__.items() if name == member.name))
    build_syntax(baseline, members)
