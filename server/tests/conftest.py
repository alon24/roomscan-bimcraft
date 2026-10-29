"""Safety nets for the whole test run.

1. A native crash (a segfault in open3d, manifold3d, embree or scipy, often in a worker thread) kills pytest before it can say
   which test it was in. faulthandler writes every thread's Python traceback to CRASH_LOG when that happens, and the NEXT run
   prints that log at the top of its report. (pytest's own faulthandler only writes to stderr, which a CI log or a `> file`
   may lose.) The log is emptied at the start of each run, so it only ever holds the last run's crash.
2. At the end of the run, wait for live-capture builds and workers to stop, so the interpreter does not tear down while a
   daemon thread is still inside native code (a classic source of a "dumped core" after every test passed)."""
import faulthandler
import threading
import time
from pathlib import Path

import pytest

CRASH_LOG = Path(__file__).resolve().parent.parent / ".pytest-crash.log"     # server/.pytest-crash.log (git-ignored)
_previous = ""
_file = None


@pytest.hookimpl(trylast=True)              # after pytest's faulthandler plugin, which points faulthandler at stderr
def pytest_configure(config):
    global _previous, _file
    try:
        _previous = CRASH_LOG.read_text()
    except OSError:
        _previous = ""
    _file = open(CRASH_LOG, "w")             # kept open: faulthandler writes to the file descriptor from a signal handler
    faulthandler.enable(file=_file, all_threads=True)


def _crash_lines() -> list[str]:
    if "Fatal Python error" not in _previous:
        return []
    return [f"THE PREVIOUS TEST RUN CRASHED IN NATIVE CODE. Its tracebacks (from {CRASH_LOG}):", *_previous.splitlines()[-80:]]


def pytest_sessionstart(session):           # written directly: `-q` hides the report header
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    for line in _crash_lines()[:1]:
        if tr is not None:
            tr.write_line(line + " (shown in full at the end)", red=True)


def pytest_terminal_summary(terminalreporter):
    lines = _crash_lines()
    if lines:
        terminalreporter.section("previous run crashed", red=True)
        for line in lines:
            terminalreporter.write_line(line)


def pytest_sessionfinish(session, exitstatus):
    try:
        from app import live
    except Exception:
        return
    t0 = time.time()
    while live.BUILDING and time.time() - t0 < 120:
        time.sleep(0.2)
    for s in list(live.SESSIONS.values()):
        s.stop()
    for t in threading.enumerate():
        if t.name.startswith("live-"):
            t.join(timeout=max(1.0, 120 - (time.time() - t0)))
