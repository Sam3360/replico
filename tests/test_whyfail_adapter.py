"""WhyFail adapter tests.

Pure unit tests for invocation extraction / gating / parsing plus one
integration test that runs the real WhyFail CLI (offline, subprocess) — the
adapter contract is the structured JSON schema WhyFail 3.x actually emits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from replico.analysis.whyfail_adapter import (
    WhyFailResult,
    _split_entries,
    describe_reason,
    find_python_invocation,
    render_diagnosis,
    run_diagnosis,
    should_diagnose,
    whyfail_available,
    whyfail_version,
)
from replico.security.redaction import Sanitizer
from replico.ui import UI

PY = "python"


class _FakeRunner:
    """Stand-in for execution.runner.Runner (argv only)."""

    def __init__(self, payload: str, returncode: int = 1) -> None:
        self.payload = payload
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd=None,
        env_extra=None,
        timeout: float = 1800,
    ):
        from replico.execution.runner import ExecResult

        self.calls.append(argv)
        return ExecResult(returncode=self.returncode, stdout=self.payload)


def _sample_cli_payload() -> str:
    return json.dumps(
        [
            {
                "context": "tests/test_auth.py::test_login [call failed]",
                "diagnostic": {
                    "whyfail": 1,
                    "schema": 2,
                    "exception_type": "KeyError",
                    "message": "'user'",
                    "chain": [],
                    "location": {"filename": "app.py", "lineno": 3, "function": "load"},
                    "diagnosis": {
                        "analyzer": "KeyError",
                        "target_description": 'response["user"]',
                        "cause": {
                            "text": "The mapping does not contain the key 'user'.",
                            "confidence": "high",
                            "insufficient_evidence": False,
                        },
                        "failure": 'response["user"] raised KeyError.',
                        "expected": ["the mapping contains the key 'user'"],
                        "actual": ["available keys: 'account', 'status'"],
                        "broken_assumptions": [],
                        "suggestions": ["Verify what response really contains."],
                        "facts": [],
                        "possible_causes": [],
                        "notes": [],
                        "provenance": [],
                        "story": [],
                    },
                    "redacted_count": 0,
                    "notes": [],
                    "call_chain": [],
                },
            }
        ]
    )


# ---------------------------------------------------------------------------
# Invocation extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script, expected",
    [
        # The interpreter is NOT part of the result — callers prepend it.
        ("python -m pytest tests/test_auth.py -q", ["-m", "pytest", "tests/test_auth.py", "-q"]),
        ("pytest tests/ -x", ["-m", "pytest", "tests/", "-x"]),
        ("python repro_fail.py", ["repro_fail.py"]),
        ("python3 -m mypkg run", ["-m", "mypkg", "run"]),
        # first python line wins inside a longer script
        ('echo "starting"\ncd src\npython -m pytest .', ["-m", "pytest", "."]),
    ],
)
def test_find_python_invocation_positive(script, expected):
    assert find_python_invocation(script, PY) == expected


def test_invocation_plus_interpreter_assembles_exactly_once():
    exe = "C:/venv/Scripts/python.exe"
    invocation = find_python_invocation("python repro_fail.py", exe)
    assert invocation == ["repro_fail.py"]
    child = [exe, *invocation]
    assert child.count(exe) == 1
    assert child == [exe, "repro_fail.py"]


@pytest.mark.parametrize(
    "script",
    [
        "python -c 'print(1)'",  # whyfail 3.x does not diagnose -c
        "python",  # bare interpreter
        "python -V",
        "npm test",
        "python -m pip install -r requirements.txt",  # install, not diagnosable target
        "echo hello",
        "",
    ],
)
def test_find_python_invocation_negative(script):
    assert find_python_invocation(script, PY) is None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _gate(**overrides) -> tuple[bool, str]:
    defaults = {
        "config_enabled": True,
        "config_whyfail": True,
        "flag": None,
        "ecosystems": ["python"],
        "local_failed": True,
        "invocation": ["python", "-m", "pytest", "tests/"],
    }
    defaults.update(overrides)
    return should_diagnose(**defaults)


def test_gate_auto_attempts_python_failure():
    attempt, reason = _gate()
    assert attempt is True
    assert reason == ""


def test_gate_rejects_when_command_passed():
    attempt, reason = _gate(local_failed=False)
    assert attempt is False
    assert reason == "no_local_failure"


def test_gate_rejects_non_python_ecosystem():
    attempt, reason = _gate(ecosystems=["node"])
    assert attempt is False
    assert reason == "unsupported_failure_type"


def test_gate_rejects_unrecognized_invocation():
    attempt, reason = _gate(invocation=None)
    assert attempt is False
    assert reason == "unsupported_failure_type"


def test_gate_rejects_when_disabled_by_flag():
    attempt, reason = _gate(flag=False)
    assert attempt is False
    assert reason == "disabled_via_flag"


def test_gate_rejects_when_disabled_in_config():
    attempt, reason = _gate(config_enabled=False)
    assert attempt is False
    assert reason == "disabled_in_config"


def test_reason_tokens_describe_human_readably():
    assert "python" in describe_reason("unsupported_failure_type").lower()


# ---------------------------------------------------------------------------
# Structured parsing
# ---------------------------------------------------------------------------


def test_parse_cli_array_preserves_context():
    entries = _split_entries(json.loads(_sample_cli_payload()))
    assert len(entries) == 1
    assert entries[0].context == "tests/test_auth.py::test_login [call failed]"
    assert entries[0].diagnostic["exception_type"] == "KeyError"


def test_parse_bare_diagnostic_dict():
    entries = _split_entries({"exception_type": "ValueError", "diagnosis": {}})
    assert len(entries) == 1
    assert entries[0].diagnostic["exception_type"] == "ValueError"


def test_run_diagnosis_success_through_runner(capsys):
    runner = _FakeRunner(_sample_cli_payload(), returncode=1)
    result = run_diagnosis(
        runner,
        python_exe=PY,
        invocation=["-m", "pytest", "tests/"],
        cwd=Path("."),
        timeout=60,
    )
    assert result.available is True
    assert result.diagnosed is True
    assert result.exit_code == 1
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0]["exception_type"] == "KeyError"
    # The wrapper argv: whyfail CLI run -> child python invocation.
    argv = runner.calls[0]
    assert argv[:6] == [sys.executable, "-m", "whyfail.cli", "run", "--no-color", "--format"]
    assert argv[7:] == [PY, "-m", "pytest", "tests/"]
    # Renderer reads the structured schema without crashing.
    render_diagnosis(UI(plain=True), result)
    out = capsys.readouterr().out
    assert "KeyError" in out
    assert "high" in out.lower()


def test_run_diagnosis_no_output_is_honest():
    runner = _FakeRunner("", returncode=1)
    result = run_diagnosis(runner, python_exe=PY, invocation=["x.py"], cwd=Path("."), timeout=60)
    assert result.diagnosed is False
    assert "no structured diagnostics" in result.reason


def test_run_diagnosis_malformed_output_is_honest():
    runner = _FakeRunner("not json at all", returncode=1)
    result = run_diagnosis(runner, python_exe=PY, invocation=["x.py"], cwd=Path("."), timeout=60)
    assert result.diagnosed is False
    assert "malformed" in result.reason


def test_run_diagnosis_zero_exit_means_no_failure():
    runner = _FakeRunner("", returncode=0)
    result = run_diagnosis(runner, python_exe=PY, invocation=["x.py"], cwd=Path("."), timeout=60)
    assert result.diagnosed is False
    assert result.exit_code == 0


def test_render_tolerates_empty_and_minimal_diagnostics(capsys):
    # A diagnostic missing optional sections must still render without error.
    result = WhyFailResult(
        available=True,
        diagnosed=True,
        diagnostics=[
            {
                "exception_type": "KeyError",
                "message": "'user'",
                "diagnosis": {"cause": {"confidence": "medium"}},
            }
        ],
    )
    render_diagnosis(UI(plain=True), result)
    out = capsys.readouterr().out
    assert "KeyError" in out or "user" in out
    assert "MEDIUM" in out


def test_render_not_available_is_graceful(capsys):
    result = WhyFailResult(available=False, reason="whyfail_unavailable")
    render_diagnosis(UI(plain=True), result)
    out = capsys.readouterr().out
    assert "WhyFail" in out


def test_render_not_diagnosed_is_graceful(capsys):
    result = WhyFailResult(available=True, diagnosed=False, reason="unsupported_failure_type")
    render_diagnosis(UI(plain=True), result)
    out = capsys.readouterr().out
    assert "no diagnosis" in out.lower()


# ---------------------------------------------------------------------------
# Real WhyFail (offline subprocess — WhyFail is a declared dependency)
# ---------------------------------------------------------------------------


def test_real_whyfail_diagnoses_local_failure(tmp_path, capsys):
    script = tmp_path / "repro_fail.py"
    script.write_text(
        "def load():\n"
        "    response = {'account': {'id': 7}, 'status': 'active'}\n"
        "    return response['user']\n"
        "load()\n",
        encoding="utf-8",
    )
    from replico.execution.runner import Runner

    runner = Runner(scratch_dir=tmp_path / "scratch")
    sanitizer = Sanitizer()
    result = run_diagnosis(
        runner,
        python_exe=sys.executable,
        invocation=[str(script)],
        cwd=tmp_path,
        timeout=120,
        sanitizer=sanitizer,
    )
    assert result.available is True
    assert result.diagnosed is True
    assert result.exit_code == 1
    diag = result.diagnostics[0]
    assert diag["exception_type"] == "KeyError"
    diagnosis = diag["diagnosis"]
    assert diagnosis["cause"]["confidence"] == "high"
    assert diagnosis["expected"]
    assert diagnosis["actual"]
    assert "user" in diagnosis["failure"]

    render_diagnosis(UI(plain=True), result)
    out = capsys.readouterr().out
    assert "Immediate failure" in out
    assert "Confidence" in out


def test_whyfail_available_and_version_match_installed():
    assert whyfail_available() is True
    version = whyfail_version()
    assert version and version.startswith("3.")


def test_whyfail_redaction_holds_at_the_adapter_boundary(tmp_path):
    """Secrets printed by the failing command never survive in diagnostics."""
    secret = "deploy-token-7f3k9q2m-local"
    script = tmp_path / "leak.py"
    script.write_text(
        "import os\n"
        "TOKEN = os.environ.get('DEPLOY_TOKEN', '')\n"
        "def boom():\n"
        "    data = {'user': TOKEN}\n"
        "    raise KeyError('user')\n"
        "boom()\n",
        encoding="utf-8",
    )
    import os

    os.environ["DEPLOY_TOKEN"] = secret
    try:
        sanitizer = Sanitizer()  # collects DEPLOY_TOKEN from the environment
        from replico.execution.runner import Runner

        result = run_diagnosis(
            Runner(scratch_dir=tmp_path / "scratch"),
            python_exe=sys.executable,
            invocation=[str(script)],
            cwd=tmp_path,
            timeout=120,
            sanitizer=sanitizer,
        )
        assert result.diagnosed is True
        blob = json.dumps(result.to_doc())
        assert secret not in blob
        assert secret not in json.dumps(result.diagnostics)
    finally:
        os.environ.pop("DEPLOY_TOKEN", None)
