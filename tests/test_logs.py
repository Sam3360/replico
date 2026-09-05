"""Log analysis tests: 500 lines of CI noise should become focused evidence."""

from __future__ import annotations

from conftest import failing_log
from replico.analysis.logs import analyze_log, split_step_segments


def _noisy(level="info"):
    lines = [f"{level}: build step {i} completed" for i in range(300)]
    lines.append("  something else happened here")
    return "\n".join(lines)


def test_pytest_failure_evidence():
    log = failing_log("tests/test_auth.py::test_login")
    evidence = analyze_log(log, source="ci")
    assert evidence.failing_tests == ["tests/test_auth.py::test_login"]
    assert evidence.category_hint == "test_failure"
    assert "AssertionError" in evidence.summary
    assert evidence.lines
    assert len(evidence.lines) <= 30
    assert evidence.source == "ci"


def test_traceback_extraction():
    log = (
        _noisy()
        + """
Traceback (most recent call last):
  File "tests/test_auth.py", line 12, in test_login
    login(client)
  File "app/auth.py", line 40, in login
    return client.post(url, data=payload)
AssertionError: expected 200, received 401
"""
    )
    evidence = analyze_log(log)
    assert evidence.category_hint == "traceback"
    assert "AssertionError" in evidence.summary


def test_import_error():
    evidence = analyze_log(_noisy() + "\nModuleNotFoundError: No module named 'urllib3'")
    assert evidence.category_hint == "import_error"
    assert "urllib3" in evidence.summary or "urllib3" in "\n".join(evidence.lines)


def test_npm_error():
    evidence = analyze_log(
        _noisy() + "\nnpm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
    )
    assert evidence.category_hint == "npm_error"


def test_timeout_marker():
    evidence = analyze_log(_noisy() + "\n##[error]The operation was canceled")
    assert evidence.category_hint in ("timeout", "error_marker")


def test_tail_fallback_when_no_structure():
    evidence = analyze_log(_noisy())
    assert evidence.category_hint == "unknown"
    assert evidence.lines  # tail shown


def test_step_segment_splitting():
    log = failing_log()
    segments = split_step_segments(log)
    names = [name for name, _lines in segments]
    assert "Run tests" in names
    assert "Run actions/checkout@v4" in names


def test_ansi_stripped():
    evidence = analyze_log("\x1b[31mFAILED tests/x.py::y - boom\x1b[0m")
    assert evidence.failing_tests == ["tests/x.py::y"]
