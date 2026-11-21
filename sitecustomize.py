"""Record dependencies in Python subprocesses during coverage generation."""

import os


def start_trace(receipt, baseline):
    import atexit
    import sys

    from dialect_probe import PROBE_SOURCE_READ
    from expression_probe import observe_expression_validation
    from factory_probe import observe_factory_calls
    from fixture_reads import fixture_dependency

    dependencies = set()
    expression_validation = observe_expression_validation(baseline, lambda: dependencies)
    factory_probe = observe_factory_calls(baseline, lambda: dependencies)
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    events = monitor.events
    recording_events = events.PY_START | events.PY_RESUME
    stdlib = os.path.dirname(os.__file__)
    opaque_launchers = (f"{stdlib}/multiprocessing/", f"{stdlib}/ctypes/")

    def enter(code, _offset):
        if factory_probe is not None:
            factory_probe.called(code, dependencies)
        if code.co_filename.startswith(baseline + "sqlglot/"):
            dependencies.add((code.co_filename[len(baseline):], code.co_firstlineno))
        elif code.co_filename.startswith(opaque_launchers):
            # Some native child launch paths do not emit subprocess audit events.
            dependencies.add(("*", 0))
        return monitor.DISABLE

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.startswith(baseline + "tests/fixtures/"):
                dependencies.add(fixture_dependency(path[len(baseline):], sys._getframe(1).f_code, baseline))
            elif path != PROBE_SOURCE_READ.get() and path.startswith(baseline + "sqlglot/"):
                dependencies.add((path[len(baseline):], 0))
        elif event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn"}:
            dependencies.add(("*", 0))

    def finish():
        if monitor.get_events(tool) != recording_events:
            return
        monitor.set_events(tool, 0)
        import json

        with open(receipt, "w", encoding="utf-8") as stream:
            json.dump({
                "pid": os.getpid(),
                "expression_validation": expression_validation,
                "factory_calls": factory_probe is not None,
                "dependencies": sorted(dependencies),
            }, stream)

    monitor.use_tool_id(tool, "blast-radius-raw-subprocess")
    monitor.register_callback(tool, events.PY_START, enter)
    monitor.register_callback(tool, events.PY_RESUME, enter)
    monitor.set_events(tool, recording_events)
    sys.addaudithook(audit)
    atexit.register(finish)


# Grandchildren without coverage must be attributed to their own launch.
receipt = os.environ.pop("BLAST_RADIUS_RAW_RECEIPT", None)
baseline = os.environ.pop("BLAST_RADIUS_RAW_BASELINE", None)
if receipt and baseline:
    start_trace(receipt, baseline)
elif os.environ.get("BLAST_RADIUS_COVERAGE_DIR"):
    from expression_probe import observe_expression_validation
    from factory_probe import observe_factory_calls

    # Process-pool initializers may run before the worker attaches test coverage.
    observe_expression_validation(os.getcwd())
    observe_factory_calls(os.getcwd())
