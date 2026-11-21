"""Profile which pinned SQLGlot source files each isolated pytest file can see."""

import argparse
import atexit
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from importlib.util import source_from_cache
import gzip
import json
from multiprocessing import util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import local
from types import ModuleType


PROBE_ROOT = "BLAST_RADIUS_IMPORT_ROOT"
PROBE_OUTPUT = "BLAST_RADIUS_IMPORT_OUTPUT"
TEST_TIMEOUT_SECONDS = 300


def install_probe(root, output):
    pid = os.getpid()
    paths = set()
    children = set()
    launching = local()
    source_prefix = str(root / "sqlglot") + os.sep

    def record_source(value):
        if not isinstance(value, (str, bytes)):
            return
        path = os.fsdecode(value)
        if not path.endswith((".py", ".pyc")):
            return
        try:
            path = os.path.abspath(path)
        except OSError:
            (output / "incomplete").touch()
            return
        if not path.startswith(source_prefix):
            return
        if path.endswith(".pyc"):
            try:
                path = source_from_cache(path)
            except ValueError:
                path = path[:-1]
        paths.add(os.path.relpath(path, root))

    def audit(event, args):
        if event == "open":
            record_source(args[0])
        elif event in {"os.exec", "os.fork", "os.forkpty", "os.system"} or (
            event == "os.posix_spawn" and not getattr(launching, "popen", False)
        ):
            # These can start or replace a process outside the launchers below.
            (output / "incomplete").touch()

    def finish():
        if os.getpid() != pid:
            return
        for module in tuple(sys.modules.values()):
            if isinstance(module, ModuleType):
                record_source(vars(module).get("__file__"))
        receipt = {"paths": sorted(paths), "children": sorted(children)}
        (output / f"{pid}.json").write_text(json.dumps(receipt), encoding="utf-8")

    popen_init = subprocess.Popen.__init__
    spawn = util.spawnv_passfds

    def track_popen(process, *args, **kwargs):
        launching.popen = True
        try:
            popen_init(process, *args, **kwargs)
        finally:
            launching.popen = False
        children.add(process.pid)

    def track_spawn(*args, **kwargs):
        # POSIX multiprocessing starts workers and the resource tracker here.
        child = spawn(*args, **kwargs)
        children.add(child)
        return child

    (output / f"{pid}.started").touch()
    atexit.register(finish)
    sys.addaudithook(audit)
    subprocess.Popen.__init__ = track_popen
    util.spawnv_passfds = track_spawn


def read_receipts(output, pid):
    if (output / "incomplete").exists():
        return None
    try:
        started = {int(path.stem) for path in output.glob("*.started")}
        receipts = {
            int(path.stem): json.loads(path.read_text(encoding="utf-8"))
            for path in output.glob("*.json")
        }
        expected = {pid}
        paths = set()
        for receipt in receipts.values():
            expected.update(receipt["children"])
            paths.update(receipt["paths"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return paths if started == receipts.keys() and expected <= started else None


def profile_file(root, probe_directory, file, output):
    output.mkdir()
    python_path = [str(probe_directory), str(root)]
    if os.environ.get("PYTHONPATH"):
        python_path.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(python_path), SKIP_INTEGRATION="0")
    env[PROBE_ROOT] = str(root)
    env[PROBE_OUTPUT] = str(output)
    log_file = output / "pytest.log"
    with log_file.open("w", encoding="utf-8") as log, subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", file],
        cwd=root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as process:
        try:
            status = process.wait(timeout=TEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            print(f"Incomplete import map for {file} (pytest timed out)", file=sys.stderr)
            return None

    if status:
        print(f"Incomplete import map for {file} (pytest exited {status})", file=sys.stderr)
        print(log_file.read_text(encoding="utf-8", errors="replace")[-1500:], file=sys.stderr)
        return None
    paths = read_receipts(output, process.pid)
    if paths is None:
        print(f"Incomplete import map for {file} (missing process receipts)", file=sys.stderr)
    return paths


def build_import_map(root, coverage_file):
    with gzip.open(coverage_file, "rt", encoding="utf-8") as stream:
        coverage = json.load(stream)
    counts = Counter(nodeid.split("::", 1)[0] for nodeid in coverage["tests"])
    files = sorted(counts, key=lambda file: (-counts[file], file))
    complete_files = []
    files_by_path = defaultdict(set)
    with TemporaryDirectory(prefix="blast-radius-imports-") as directory:
        probe_directory = Path(directory)
        # Python automatically runs sitecustomize in the pytest process and children.
        shutil.copyfile(__file__, probe_directory / "sitecustomize.py")
        with ThreadPoolExecutor(max_workers=min(os.process_cpu_count() or 1, 3)) as executor:
            jobs = {
                file: executor.submit(profile_file, root, probe_directory, file, probe_directory / str(index))
                for index, file in enumerate(files)
            }
            for file, job in jobs.items():
                paths = job.result()
                if paths is not None:
                    complete_files.append(file)
                    for path in paths:
                        files_by_path[path].add(file)
    print(f"Profiled {len(complete_files)}/{len(files)} test files", file=sys.stderr)
    return {
        "complete_files": sorted(complete_files),
        "files_by_path": {path: sorted(files) for path, files in sorted(files_by_path.items())},
    }


def main():
    from factory_metadata import build_factory_metadata

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    payload = build_import_map(root, args.coverage)
    payload["factories"] = build_factory_metadata(root, payload["files_by_path"])
    args.output.write_bytes(gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"), mtime=0
    ))


if __name__ == "sitecustomize" and (directory := os.environ.get(PROBE_OUTPUT)):
    install_probe(Path(os.environ[PROBE_ROOT]), Path(directory))
elif __name__ == "__main__":
    main()
