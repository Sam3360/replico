"""WhyFail integration adapter.

Replico is the *reproduction* engine; WhyFail is the *diagnosis* engine.
This module is the only place Replico talks to WhyFail, isolating the real
WhyFail 3.x interface behind a small surface so that a missing, incompatible
or unhelpful WhyFail can never turn a successful reproduction into a failure.

How it works
------------
Replico executes the failing command with its own runner (exit code + full
output are preserved, exactly as in v0.1). When the local reproduction of a
*Python* failure is eligible, the adapter runs the same python/pytest
invocation a second time through WhyFail's structured CLI:

    python -m whyfail.cli run --no-color --format json <python> <script/module>

WhyFail 3.0.0 recognizes ``python <script>``, ``python -m <module>`` and
``pytest`` invocations (not ``python -c``), preserves the child exit status,
suppresses child output in JSON mode and prints one structured diagnostic per
failing test as ``[{"context", "diagnostic"}]``. The diagnostics arrive as
WhyFail's own ``Diagnostic.to_dict()`` schema — Replico consumes that
structure directly and never scrapes pretty terminal output.

Security
--------
WhyFail redacts runtime values internally (SecretShield). Replico still
passes every diagnostic through its own Sanitizer before display or
persistence — the Redact layer stays in charge of the final boundary.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from replico.security.redaction import Sanitizer

# The only child invocations WhyFail 3.0.0 can diagnose.
_PYTHON_EXECUTABLES = ("python", "python3", "py", "pypy", "pypy3", "python.exe")
_MODULE_EXCLUDE = {"-c", "pip", "pip3", "venv", "ensurepip"}
_SCRIPT_RE = re.compile(r"^[\w./\\-]+\.py$", re.IGNORECASE)


@dataclass
class WhyFailResult:
    """Outcome of one WhyFail diagnosis attempt (source of truth for callers).

    ``diagnostics`` holds the raw WhyFail ``Diagnostic.to_dict()`` structures
    (already redacted by WhyFail, re-sanitized by Replico). ``available`` and
    ``diagnosed`` are deliberately separate: a fully installed WhyFail that
    has nothing to say yields ``available=True, diagnosed=False``.
    """

    available: bool = False
    diagnosed: bool = False
    exit_code: int | None = None
    reason: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    version: str | None = None
    source: str = "local_reproduction"
    child_command: list[str] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        """Machine-readable representation (safe for JSON/storage after
        sanitization by the caller's security layer)."""
        return {
            "tool": "whyfail",
            "version": self.version,
            "available": self.available,
            "diagnosed": self.diagnosed,
            "reason": self.reason,
            "source": self.source,
            "exit_code": self.exit_code,
            "diagnostics": self.diagnostics,
        }

    def summary(self) -> dict[str, Any] | None:
        """Tiny summary of the primary diagnostic (or None)."""
        if not self.diagnostics:
            return None
        primary = self.diagnostics[0]
        diagnosis = primary.get("diagnosis") or {}
        cause = diagnosis.get("cause") or {}
        location = primary.get("location") or {}
        return {
            "exception_type": primary.get("exception_type"),
            "message": primary.get("message"),
            "analyzer": diagnosis.get("analyzer"),
            "confidence": cause.get("confidence"),
            "context": self._context_of(primary),
            "location": {
                "filename": location.get("filename"),
                "lineno": location.get("lineno"),
                "function": location.get("function"),
            },
        }

    @staticmethod
    def _context_of(diagnostic: dict[str, Any]) -> str | None:
        # The CLI wraps each diagnostic with {"context", "diagnostic"}; when a
        # caller stored the inner dict, the context is lost — callers that
        # care pass entries through _entry() helpers below.
        return None


@dataclass
class DiagnosisEntry:
    """One CLI entry: a context label plus the inner diagnostic dict."""

    context: str | None
    diagnostic: dict[str, Any]


def _split_entries(payload: Any) -> list[DiagnosisEntry]:
    """Normalize WhyFail CLI JSON into entries, tolerating both the CLI's
    ``[{"context", "diagnostic"}]`` array and a bare diagnostic dict."""
    entries: list[DiagnosisEntry] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            inner = item.get("diagnostic")
            if isinstance(inner, dict):
                entries.append(DiagnosisEntry(context=item.get("context"), diagnostic=inner))
            else:
                entries.append(DiagnosisEntry(context=None, diagnostic=item))
    elif isinstance(payload, dict) and "exception_type" in payload:
        entries.append(DiagnosisEntry(context=None, diagnostic=payload))
    return entries


def _diagnosis_of(diagnostic: dict[str, Any]) -> dict[str, Any]:
    inner = diagnostic if "diagnosis" in diagnostic else {}
    return inner.get("diagnosis") or {}


def primary_confidence(diagnostics: list[DiagnosisEntry]) -> str | None:
    if not diagnostics:
        return None
    cause = _diagnosis_of(diagnostics[0].diagnostic).get("cause") or {}
    return cause.get("confidence")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def whyfail_available() -> bool:
    """True when WhyFail can be run from Replico's interpreter."""
    return _whyfail_prefix() is not None


def whyfail_version() -> str | None:
    try:
        import importlib.metadata

        return importlib.metadata.version("whyfail")
    except Exception:  # noqa: BLE001 - metadata absence must not crash
        return None


def _whyfail_prefix() -> list[str] | None:
    """How to invoke the WhyFail CLI: ``[interpreter, -m, whyfail.cli]``.

    Running through ``python -m`` keeps the wrapper independent of console
    scripts / PATH and guarantees it uses the interpreter Replico is running
    under (which is where WhyFail is installed as a dependency).
    """
    try:
        import whyfail  # type: ignore[import-untyped]  # noqa: F401

        return [sys.executable, "-m", "whyfail.cli"]
    except Exception:  # noqa: BLE001 - ImportError or init failure
        return None


# ---------------------------------------------------------------------------
# Invocation extraction
# ---------------------------------------------------------------------------


def find_python_invocation(script: str, python_exe: str) -> list[str] | None:
    """Find a WhyFail-eligible python/pytest invocation inside a shell script.

    GitHub ``run:`` blocks are shell scripts, so we scan their lines for the
    first command WhyFail 3.x can actually diagnose:

    * ``python[3] [-m <module>] <script.py> [args]`` — scripts and modules;
    * ``python[3] -m pytest [args]`` and ``pytest [args]`` — pytest runs.

    ``python -c``, bare ``python`` and non-Python commands are rejected (they
    are either not diagnosable or not Python). Returns the invocation argv
    *without* the interpreter — the caller supplies ``python_exe`` and
    assembles ``[python_exe, *invocation]``.
    """
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "cd ", "export ", "echo ", "sudo ")):
            continue
        try:
            tokens = shlex.split(stripped, posix=sys.platform != "win32")
        except ValueError:
            continue
        if not tokens:
            continue
        head = tokens[0].lower()
        head_exe = head.rsplit(".exe", 1)[0] if head.endswith(".exe") else head

        if head_exe == "pytest":
            return ["-m", "pytest", *tokens[1:]]

        if head_exe not in _PYTHON_EXECUTABLES:
            continue
        body = tokens[1:]
        if not body:
            continue  # bare `python`
        if body[0] == "-m":
            if len(body) < 2 or body[1] in _MODULE_EXCLUDE:
                continue
            return ["-m", *body[1:]]
        if body[0] == "-c":
            continue
        if _SCRIPT_RE.match(body[0]):
            return body
        continue
    return None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


# Stable reason tokens used in JSON docs and status output. Short tokens keep
# machine output parseable; REASON_TEXT maps them to human sentences.
REASON_TEXT = {
    "unsupported_failure_type": "not a Python failure Replico can diagnose",
    "disabled_in_config": "diagnostics disabled in configuration",
    "whyfail_disabled_in_config": "whyfail disabled in configuration",
    "disabled_via_flag": "disabled via --no-diagnose",
    "no_local_failure": "no local failure to diagnose",
    "whyfail_unavailable": "whyfail integration could not be initialized",
}


def describe_reason(reason: str) -> str:
    return REASON_TEXT.get(reason, reason)


def should_diagnose(
    *,
    config_enabled: bool,
    config_whyfail: bool,
    flag: bool | None,
    ecosystems: list[str],
    local_failed: bool,
    invocation: list[str] | None,
) -> tuple[bool, str]:
    """Decide whether to attempt WhyFail diagnosis.

    ``flag`` is the ``--diagnose``/``--no-diagnose`` CLI switch (None = auto).
    Returns (attempt, reason-token-if-not).
    """
    if not config_enabled:
        return False, "disabled_in_config"
    if not config_whyfail:
        return False, "whyfail_disabled_in_config"
    if flag is False:
        return False, "disabled_via_flag"
    if not local_failed:
        return False, "no_local_failure"
    if "python" not in ecosystems:
        return False, "unsupported_failure_type"
    if invocation is None:
        return False, "unsupported_failure_type"
    if not whyfail_available():
        return False, "whyfail_unavailable"
    return True, ""


TOOL_MARKER = "##[replico-whyfail]"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class _RunnerLike(Protocol):
    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path | str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 1800,
    ) -> Any: ...


def run_diagnosis(
    runner: _RunnerLike,
    *,
    python_exe: str,
    invocation: list[str],
    cwd: Path,
    timeout: float,
    sanitizer: Sanitizer | None = None,
    extra_env: dict[str, str] | None = None,
) -> WhyFailResult:
    """Run ``whyfail run --format json`` over a python/pytest invocation.

    The wrapper itself runs under Replico's interpreter (where WhyFail is
    installed); the *child* runs under the reproduction interpreter passed in
    ``python_exe`` (the venv / runtime python), so the failing code is
    executed in the same environment that reproduced the CI failure.
    """
    result = WhyFailResult(
        available=whyfail_available(),
        version=whyfail_version(),
        source="local_reproduction",
        child_command=[python_exe, *invocation],
    )
    prefix = _whyfail_prefix()
    if prefix is None:
        result.reason = "whyfail integration could not be initialized"
        return result

    argv = [*prefix, "run", "--no-color", "--format", "json", python_exe, *invocation]
    exec_result = runner.run_argv(argv, cwd=cwd, env_extra=extra_env, timeout=timeout)
    result.exit_code = exec_result.returncode
    if exec_result.launch_error:
        result.reason = f"whyfail could not run: {exec_result.launch_error}"
        return result
    if exec_result.returncode == 0:
        result.reason = "command passed — no failure to diagnose"
        return result

    out = (exec_result.stdout or "").strip()
    if not out:
        note = (exec_result.stderr or "").strip()
        result.reason = "no structured diagnostics produced" + (f" ({note[:200]})" if note else "")
        return result

    try:
        payload = json.loads(out)
    except ValueError as exc:
        result.reason = f"malformed whyfail output ({exc})"
        return result

    entries = _split_entries(payload)
    if not entries:
        result.reason = "whyfail produced no diagnostics"
        return result

    result.diagnostics = [entry.diagnostic for entry in entries]
    if sanitizer is not None:
        result.diagnostics = _sanitize_diagnostics(result.diagnostics, sanitizer)
    result.diagnosed = True
    result.reason = ""
    return result


def _sanitize_diagnostics(
    diagnostics: list[dict[str, Any]], sanitizer: Sanitizer
) -> list[dict[str, Any]]:
    import copy

    out: list[dict[str, Any]] = []

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str):
            return sanitizer.redact(value)
        return value

    for diagnostic in diagnostics:
        out.append(walk(copy.deepcopy(diagnostic)))
    return out


# ---------------------------------------------------------------------------
# Rendering (compact, structured, evidence-based)
# ---------------------------------------------------------------------------


def render_diagnosis(ui, result: WhyFailResult, *, divider: bool = True) -> None:
    """Render a diagnosis report from the real WhyFail structured data.

    Only reads fields WhyFail 3.x actually emits (``diagnosis.failure``,
    ``diagnosis.cause``, ``expected``/``actual``, ``broken_assumptions``,
    ``suggestions``, ``chain``/``call_chain``, ``location``). Nothing here is
    invented: absent fields are simply not shown.
    """
    if divider:
        ui.rule("WHYFAIL DIAGNOSIS")
    else:
        ui.out("WHYFAIL DIAGNOSIS", style="bold cyan")

    if not result.available:
        ui.out("WhyFail integration could not be initialized — the reproduction result stands.")
        return
    if not result.diagnosed:
        ui.out(f"WhyFail: no diagnosis ({describe_reason(result.reason) or 'not applicable'}).")
        return

    count = len(result.diagnostics)
    ui.out(
        f"{count} diagnostic(s) from the local reproduction"
        + (f" (WhyFail {result.version})" if result.version else ""),
        style="dim",
    )
    for index, diagnostic in enumerate(result.diagnostics, start=1):
        if count > 1:
            ui.out("")
            label = diagnostic.get("context")
            ui.out(f"Failure {index}" + (f" — {label}" if label else ""), style="bold")
        _render_one(ui, diagnostic)
    if result.exit_code is not None:
        ui.kv("Exit code", str(result.exit_code))


def _render_one(ui, diagnostic: dict[str, Any]) -> None:
    diagnosis = _diagnosis_of(diagnostic)
    cause = diagnosis.get("cause") or {}

    failure = diagnosis.get("failure") or diagnostic.get("message")
    if failure:
        ui.out("")
        ui.kv("Immediate failure", str(failure))

    cause_text = cause.get("text")
    if cause_text:
        ui.out("")
        ui.kv("Likely cause", str(cause_text))

    expected = diagnosis.get("expected") or []
    actual = diagnosis.get("actual") or []
    if expected or actual:
        ui.out("")
        if expected:
            ui.kv("Expected", expected[0])
            for extra in expected[1:]:
                ui.out(f"          {extra}")
        if actual:
            ui.kv("Actual", actual[0])
            for extra in actual[1:]:
                ui.out(f"          {extra}")

    assumptions = diagnosis.get("broken_assumptions") or []
    if assumptions:
        ui.out("")
        ui.kv("Broken assumption", str(assumptions[0].get("assumption", "")))

    chain = diagnostic.get("call_chain") or []
    if chain:
        ui.out("")
        ui.out("Execution path:")
        for entry in chain[:10]:
            function = entry.get("function") or "?"
            filename = str(entry.get("filename") or "").replace("\\", "/")
            if filename != "<string>" and filename:
                filename = filename.rsplit("/", 1)[-1]
            role = entry.get("role")
            marker = " ← failure" if role == "raised" else ""
            repeats = entry.get("repeats")
            suffix = f" (x{repeats})" if isinstance(repeats, int) and repeats > 1 else ""
            ui.out(f"    {function}({filename}){marker}{suffix}")

    confidence = cause.get("confidence")
    if confidence:
        ui.out("")
        ui.kv("Confidence", str(confidence).upper())

    suggestions = diagnosis.get("suggestions") or []
    if suggestions:
        ui.out("")
        ui.out("What to investigate:")
        for suggestion in suggestions[:4]:
            ui.out(f"  • {suggestion}")

    location = diagnostic.get("location") or {}
    if location:
        ui.out("")
        ui.kv(
            "Failure location",
            f"{location.get('filename')}:{location.get('lineno')} in "
            f"{location.get('function') or '?'}",
        )
    ui.out("")
    ui.out("Evidence: observed locally during reproduction (CI evidence is separate)", style="dim")
