"""Pytest plugin that records dependencies on the pinned, unmodified checkout."""

import gzip
import json
import os
import pickle
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest
from expression_probe import observe_expression_validation
from factory_probe import observe_factory_calls

_PROCESS_COVERAGE = None
RAW_RECEIPT = "BLAST_RADIUS_RAW_RECEIPT"
RAW_BASELINE = "BLAST_RADIUS_RAW_BASELINE"


def run_traced_task(nodeid, payload):
    global _PROCESS_COVERAGE
    if _PROCESS_COVERAGE is None:
        _PROCESS_COVERAGE = Coverage()
        Finalize(None, _PROCESS_COVERAGE.write, args=(f"child-{os.getpid()}",), exitpriority=1)
    if _PROCESS_COVERAGE.nodeid != nodeid:
        _PROCESS_COVERAGE.start_test(nodeid)
    task, args, kwargs = pickle.loads(payload)
    return task(*args, **kwargs)


class Coverage:
    def __init__(self):
        self.root = str(Path.cwd()) + "/"
        self.source = self.root + "sqlglot/"
        self.fixtures = self.root + "tests/fixtures/"
        self.collection = set()
        self.current = self.collection
        self.nodeid = None
        self.tests = {}
        self.shared = defaultdict(set)
        self.failed = set()
        self.output = Path(os.environ["BLAST_RADIUS_COVERAGE_DIR"])
        self.active = True
        self.raw_children = {}
        self.factory_probe = observe_factory_calls(self.root, lambda: self.current)
        self.expression_validation = observe_expression_validation(self.root, lambda: self.current)
        self.monitoring = monitoring = sys.monitoring
        monitoring.use_tool_id(monitoring.COVERAGE_ID, "blast-radius")
        monitoring.register_callback(monitoring.COVERAGE_ID, monitoring.events.PY_START, self.enter)
        monitoring.register_callback(monitoring.COVERAGE_ID, monitoring.events.PY_RESUME, self.enter)
        monitoring.set_events(monitoring.COVERAGE_ID, monitoring.events.PY_START | monitoring.events.PY_RESUME)
        sys.addaudithook(self.audit)

    def enter(self, code, _offset):
        if self.factory_probe is not None:
            self.factory_probe.called(code, self.current)
        if code.co_filename.startswith(self.source):
            self.current.add((code.co_filename[len(self.root):], code.co_firstlineno))
        return self.monitoring.DISABLE

    def audit(self, event, args):
        if not self.active:
            return
        if event == "open" and isinstance(args[0], str):
            path = args[0]
            if path.startswith(self.fixtures) or path.startswith("tests/fixtures/"):
                self.current.add((path.removeprefix(self.root), 0))
            elif path.startswith(self.source) and path.endswith(".py"):
                caller = sys._getframe(1).f_code.co_filename
                if not caller.startswith("<frozen importlib"):
                    self.current.add((path[len(self.root):], 0))
        elif event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn"}:
            # Child processes can run code that Python's monitor cannot see.
            if event in {"subprocess.Popen", "os.posix_spawn"}:
                environment = args[-1] or {}
                if environment.get(RAW_RECEIPT) in self.raw_children:
                    return
            self.current.add(("*", 0))

    def trace_subprocesses(self):
        initialize = subprocess.Popen.__init__

        def traced_initialize(process, args, *positional, **options):
            environment = options.get("env")
            if environment is None:
                environment = os.environ
            if (
                positional
                or not isinstance(args, (tuple, list))
                or not args
                or any(options.get(key) is not None for key in ("cwd", "executable", "preexec_fn"))
                or options.get("shell")
            ):
                return initialize(process, args, *positional, **options)
            path = shutil.which(os.fsdecode(args[0]), path=environment.get("PATH", os.defpath))
            if path is None or os.path.realpath(path) != os.path.realpath(sys.executable):
                return initialize(process, args, *positional, **options)

            receipt = str(self.output.resolve() / f"raw-{os.getpid()}-{len(self.raw_children)}.trace")
            self.raw_children[receipt] = process, self.current
            options["env"] = dict(environment, **{RAW_RECEIPT: receipt, RAW_BASELINE: self.root})
            try:
                return initialize(process, args, **options)
            except BaseException:
                self.raw_children.pop(receipt)
                self.current.add(("*", 0))
                raise

        subprocess.Popen.__init__ = traced_initialize

    def collect_raw_children(self):
        for receipt, (process, scope) in self.raw_children.items():
            try:
                trace = json.loads(Path(receipt).read_text(encoding="utf-8"))
                if (
                    process.poll() != 0
                    or trace["pid"] != process.pid
                    or not trace.get("expression_validation")
                    or (self.factory_probe is not None and not trace.get("factory_calls"))
                ):
                    raise ValueError("incomplete child trace")
                scope.update(map(tuple, trace["dependencies"]))
            except (OSError, ValueError, KeyError, TypeError):
                scope.add(("*", 0))

    def trace_process_pools(self):
        submit = ProcessPoolExecutor.submit

        def traced_submit(pool, fn, /, *args, **kwargs):
            # Deserialize arguments inside the traced task as well.
            payload = pickle.dumps((fn, args, kwargs))
            return submit(pool, run_traced_task, self.nodeid, payload)

        ProcessPoolExecutor.submit = traced_submit

    def start_test(self, nodeid):
        self.nodeid = nodeid
        self.current = self.tests.setdefault(nodeid, set()) if nodeid else self.collection
        self.monitoring.restart_events()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.start_test(item.nodeid)
        try:
            return (yield)
        finally:
            self.nodeid = None
            self.current = self.collection
            self.monitoring.restart_events()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        yield from self.record_shared(item)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item):
        yield from self.record_shared(item)

    def record_shared(self, item):
        current = self.current
        self.current = self.shared[item.nodeid.split("::", 1)[0]]
        self.monitoring.restart_events()
        try:
            return (yield)
        finally:
            self.current = current
            self.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        if not report.passed:
            self.failed.add(report.nodeid)

    def pytest_sessionfinish(self, session):
        worker = getattr(session.config, "workerinput", {}).get("workerid", "main")
        self.write(worker)

    def write(self, worker):
        self.monitoring.set_events(self.monitoring.COVERAGE_ID, 0)
        self.active = False
        self.collect_raw_children()
        path = self.output / f"{worker}.json"
        path.write_text(json.dumps({
            "expression_validation": self.expression_validation,
            "dialect_fields": self.expression_validation,
            "identity_comparisons": self.expression_validation,
            "guard_coverage": self.expression_validation,
            "literal_coverage": self.expression_validation,
            "factory_calls": self.factory_probe is not None,
            "collection": sorted(self.collection),
            "tests": {
                nodeid: sorted(dependencies | self.shared[nodeid.split("::", 1)[0]])
                for nodeid, dependencies in self.tests.items()
                if nodeid not in self.failed
            },
        }, separators=(",", ":")))


def pytest_configure(config):
    if not config.getoption("numprocesses", default=0) or hasattr(config, "workerinput"):
        coverage = Coverage()
        coverage.trace_process_pools()
        coverage.trace_subprocesses()
        config.pluginmanager.register(coverage)


def pack_coverage(directory, output):
    """Merge worker and child-process coverage into one reproducible artifact."""
    collection = set()
    tests = {}
    child_tests = defaultdict(set)
    expression_validation = True
    dialect_fields = True
    identity_comparisons = True
    guard_coverage = True
    literal_coverage = True
    factory_calls = True
    for path in directory.glob("*.json"):
        shard = json.loads(path.read_text())
        expression_validation &= shard.get("expression_validation") is True
        dialect_fields &= shard.get("dialect_fields") is True
        identity_comparisons &= shard.get("identity_comparisons") is True
        guard_coverage &= shard.get("guard_coverage") is True
        literal_coverage &= shard.get("literal_coverage") is True
        factory_calls &= shard.get("factory_calls") is True
        collection.update(map(tuple, shard["collection"]))
        for nodeid, dependencies in shard["tests"].items():
            if path.name.startswith("child-"):
                child_tests[nodeid].update(map(tuple, dependencies))
            else:
                tests[nodeid] = set(map(tuple, dependencies))

    tests = {nodeid: dependencies | child_tests[nodeid] for nodeid, dependencies in tests.items()}
    dependency_table = sorted(collection.union(*tests.values()))
    dependency_numbers = {dependency: index for index, dependency in enumerate(dependency_table)}
    payload = {
        "expression_validation": bool(tests) and expression_validation,
        "dialect_fields": bool(tests) and dialect_fields,
        "identity_comparisons": bool(tests) and identity_comparisons,
        "guard_coverage": bool(tests) and guard_coverage,
        "literal_coverage": bool(tests) and literal_coverage,
        "factory_calls": bool(tests) and factory_calls,
        "dependencies": dependency_table,
        "collection": sorted(dependency_numbers[dependency] for dependency in collection),
        "tests": {
            nodeid: sorted(dependency_numbers[dependency] for dependency in dependencies)
            for nodeid, dependencies in sorted(tests.items())
        },
    }
    output.write_bytes(gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"), mtime=0
    ))


if __name__ == "__main__":
    pack_coverage(Path(sys.argv[1]), Path(sys.argv[2]))
