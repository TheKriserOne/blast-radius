"""Pytest plugin that records dependencies on the pinned, unmodified checkout."""

import gzip
import json
import os
import pickle
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.util import Finalize
from pathlib import Path

import pytest

_PROCESS_COVERAGE = None


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
        self.monitoring = monitoring = sys.monitoring
        monitoring.use_tool_id(monitoring.COVERAGE_ID, "blast-radius")
        monitoring.register_callback(monitoring.COVERAGE_ID, monitoring.events.PY_START, self.enter)
        monitoring.register_callback(monitoring.COVERAGE_ID, monitoring.events.PY_RESUME, self.enter)
        monitoring.set_events(monitoring.COVERAGE_ID, monitoring.events.PY_START | monitoring.events.PY_RESUME)
        sys.addaudithook(self.audit)

    def enter(self, code, _offset):
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
        elif event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"}:
            # Child processes can run code that Python's monitor cannot see.
            self.current.add(("*", 0))

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
        path = self.output / f"{worker}.json"
        path.write_text(json.dumps({
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
        config.pluginmanager.register(coverage)


def pack_coverage(directory, output):
    """Merge worker and child-process coverage into one reproducible artifact."""
    collection = set()
    tests = {}
    child_tests = defaultdict(set)
    for path in directory.glob("*.json"):
        shard = json.loads(path.read_text())
        collection.update(map(tuple, shard["collection"]))
        for nodeid, dependencies in shard["tests"].items():
            if path.name.startswith("child-"):
                child_tests[nodeid].update(map(tuple, dependencies))
            else:
                tests[nodeid] = set(map(tuple, dependencies))

    payload = {
        "collection": sorted(collection),
        "tests": {
            nodeid: sorted(dependencies | child_tests[nodeid])
            for nodeid, dependencies in sorted(tests.items())
        },
    }
    output.write_bytes(gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"), mtime=0
    ))


if __name__ == "__main__":
    pack_coverage(Path(sys.argv[1]), Path(sys.argv[2]))
