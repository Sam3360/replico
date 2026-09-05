"""Smart log analysis.

Turns hundreds of lines of CI output into a handful of relevant lines.
These functions are pure (no I/O) so they can be unit tested with fixture
logs. Redaction happens at display/persistence time, not here.
"""

from __future__ import annotations

import re

from replico.models import FailureEvidence

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_GROUP_RE = re.compile(r"^##\[group\](.*)$")
_GROUP_END_RE = re.compile(r"^##\[endgroup\]")

_FAILED_TEST_RE = re.compile(r"^FAILED\s+([^\s]+?)(?:\s+-|$)")
_FAILED_TEST_SUMMARY_RE = re.compile(r"^=+.*failed.*=+$")

_MAX_RELEVANT_LINES = 30
_CONTEXT_BEFORE = 3
_CONTEXT_AFTER = 12


def strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def split_step_segments(log: str) -> list[tuple[str | None, list[str]]]:
    """Split a job log into per-step segments using GitHub's group markers.

    Returns ``[(step_name_or_None, [lines...])]``. When markers are absent a
    single segment covers the whole log.
    """
    segments: list[tuple[str | None, list[str]]] = []
    current_name: str | None = None
    current: list[str] = []
    for raw in log.splitlines():
        line = strip_ansi(raw)
        match = _GROUP_RE.match(line)
        if match:
            if current or current_name is not None:
                segments.append((current_name, current))
            current = []
            current_name = match.group(1).strip() or None
            continue
        if _GROUP_END_RE.match(line):
            if current or current_name is not None:
                segments.append((current_name, current))
            current = []
            current_name = None
            continue
        current.append(line)
    if current or current_name is not None:
        segments.append((current_name, current))
    if not segments:
        segments.append((None, [strip_ansi(line) for line in log.splitlines()]))
    return segments


class _Finding:
    __slots__ = ("kind", "line", "snippet")

    def __init__(self, kind: str, line: int, snippet: list[str]) -> None:
        self.kind = kind
        self.line = line
        self.snippet = snippet


def _python_traceback_finding(lines: list[str]) -> _Finding | None:
    """Last traceback: exception summary + the frame that raised it."""
    trace_indexes = [
        i for i, line in enumerate(lines) if "Traceback (most recent call last)" in line
    ]
    if not trace_indexes:
        return None
    start = trace_indexes[-1]
    snippet: list[str] = []
    for i in range(start + 1, min(len(lines), start + 40)):
        line = lines[i]
        if not line.strip():
            if snippet and snippet[-1].strip() == "":
                break
            snippet.append(line)
            continue
        if (
            re.match(r"^\s*File \"", line)
            and snippet
            and re.match(r"^\s*(File \"|Traceback)", snippet[-1])
        ):
            # another frame; keep appending lines but only the last matters
            pass
        snippet.append(line)
        if re.match(r"^(?:[A-Za-z_][\w.]*\.)*[A-Za-z_][\w]*Error", line.strip()):
            break
    snippet[-1].strip() if snippet else ""
    return _Finding("traceback", start, snippet[:15])


def _findings_in(lines: list[str]) -> list[_Finding]:
    findings: list[_Finding] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("##[group", "##[endgroup", "::")):
            continue
        if stripped.startswith("##[error]"):
            findings.append(_Finding("error_marker", i, [stripped[:400]]))
            continue
        # pytest failure summary: FAILED path - reason
        if _FAILED_TEST_RE.match(stripped):
            snippet = lines[max(0, i - 1) : i + 1]
            findings.append(_Finding("test_failure", i, snippet))
        elif stripped.startswith("ModuleNotFoundError") or stripped.startswith("ImportError"):
            findings.append(_Finding("import_error", i, [lines[max(0, i - 2)], line]))
        elif stripped.startswith("##[error]"):
            findings.append(_Finding("error_marker", i, [line[:400]]))
        elif re.match(r"^Error: Process completed with exit code", stripped):
            findings.append(_Finding("exit_code", i, [line]))
        elif re.search(r"command not found|is not recognized|No such file or directory", stripped):
            findings.append(_Finding("missing_command", i, [line]))
        elif re.search(
            r"permission denied|PermissionError|Access is denied|denied \(publickey\)", stripped
        ):
            findings.append(_Finding("permission", i, [line]))
        elif re.search(
            r"timed? ?out|The operation was canceled|OperationCanceledException", stripped, re.I
        ):
            findings.append(_Finding("timeout", i, [line]))
        elif stripped.startswith("npm ERR!") or re.match(r"^npm error ", stripped):
            findings.append(_Finding("npm_error", i, [line[:400]]))
        elif re.match(r"^error(\[|:)", stripped, re.I):
            findings.append(_Finding("compiler_error", i, [line[:400]]))
        elif re.search(
            r"error: .*Failed to download|Could not find a version|ResolutionImpossible|No matching distribution",
            stripped,
        ):
            findings.append(_Finding("dependency_error", i, [line[:400]]))
        elif stripped.startswith("Killed") or "Segmentation fault" in stripped:
            findings.append(_Finding("crash", i, [line]))
    tb = _python_traceback_finding(lines)
    if tb is not None:
        findings.append(tb)
    return findings


_STRONG = {
    "test_failure",
    "import_error",
    "dependency_error",
    "timeout",
    "crash",
    "permission",
}


def _failing_tests(lines: list[str]) -> list[str]:
    tests: list[str] = []
    for line in lines:
        match = _FAILED_TEST_RE.match(line.strip())
        if match:
            tests.append(match.group(1))
    return list(dict.fromkeys(tests))


def _summary_for(lines: list[str], kind: str) -> str:
    compact = [re.sub(r"\s+", " ", line).strip() for line in lines if line.strip()]
    if kind == "test_failure":
        return compact[-1][:300] if compact else ""
    if kind == "traceback":
        # exception line
        return compact[-1][:300] if compact else ""
    if kind == "exit_code":
        return compact[-1][:200] if compact else ""
    return compact[0][:300] if compact else ""


def analyze_log(log: str, source: str = "") -> FailureEvidence:
    """Condense a raw CI log into focused, relevant evidence."""
    lines = [strip_ansi(line) for line in log.splitlines()]
    tests = _failing_tests(lines)
    findings = _findings_in(lines)

    evidence = FailureEvidence(source=source, failing_tests=tests)
    if not findings:
        tail = [line for line in lines[-12:] if line.strip()]
        if tail:
            evidence.lines = tail
            evidence.line_numbers = list(range(len(lines) - len(tail) + 1, len(lines) + 1))
            evidence.summary = "no structured error found; showing the tail of the log"
            evidence.category_hint = "unknown"
        return evidence

    # Pick the strongest finding, preferring a pytest failure / traceback.
    def rank(f: _Finding) -> tuple[int, int]:
        if f.kind in _STRONG:
            return (0, 0 if f.kind == "test_failure" else 1)
        return (1, 0)

    findings.sort(key=lambda f: (rank(f)[0], f.line))
    primary = findings[0]
    window: list[str] = []
    indexes: list[int] = []
    start = max(0, primary.line - _CONTEXT_BEFORE)
    end = min(len(lines), primary.line + _CONTEXT_AFTER)
    for idx in range(start, end):
        if lines[idx].strip() or idx == primary.line:
            window.append(lines[idx])
            indexes.append(idx + 1)
        if len(window) >= _MAX_RELEVANT_LINES:
            break
    evidence.lines = window
    evidence.line_numbers = indexes
    evidence.category_hint = primary.kind
    evidence.summary = _summary_for(primary.snippet or window, primary.kind)
    return evidence
