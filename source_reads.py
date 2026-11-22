"""Distinguish file reads made by stock imports from explicit source reads."""

from importlib import _bootstrap
from importlib.machinery import SourceFileLoader
from os.path import realpath
from platform import _Processor
from shutil import which

from dialect_probe import PROBE_SOURCE_READ
from expression_probe import execute_expression_module

_IMPORT_FRAMES = (SourceFileLoader.get_data.__code__, SourceFileLoader.get_code.__code__,
                  SourceFileLoader.exec_module.__code__)
_IMPORT_EXECUTORS = (_bootstrap._load_unlocked.__code__, _bootstrap._exec.__code__)


def explicit_source_read(path, frame):
    if path == PROBE_SOURCE_READ.get():
        return False

    if type(frame.f_locals.get("self")) is not SourceFileLoader:
        return True
    for code in _IMPORT_FRAMES:
        if frame is None or frame.f_code is not code:
            return True
        frame = frame.f_back

    if frame is not None and frame.f_code is execute_expression_module.__code__:
        frame = frame.f_back
    return frame is None or not any(frame.f_code is code for code in _IMPORT_EXECUTORS)


def platform_processor_probe(event, args, frame):
    """xdist queries the machine architecture before it collects any tests."""
    if event != "subprocess.Popen" or args != ("uname", ["uname", "-p"], None, None):
        return False
    executable = which("uname")
    if executable is None or realpath(executable) != "/usr/bin/uname":
        return False
    caller = _Processor.from_subprocess.__code__
    while frame is not None:
        if frame.f_code is caller:
            return True
        frame = frame.f_back
    return False
