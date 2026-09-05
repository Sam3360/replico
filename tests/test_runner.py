"""Process runner tests: argv safety, script shells, env hygiene, timeouts."""

from __future__ import annotations

import sys
from pathlib import Path

from replico.execution.runner import Runner


def _runner(tmp_path: Path) -> Runner:
    return Runner(scratch_dir=tmp_path / "scratch")


def test_run_argv_success(tmp_path):
    result = _runner(tmp_path).run_argv([sys.executable, "-c", "print('hi')"])
    assert result.ok
    assert "hi" in result.stdout


def test_run_argv_failure(tmp_path):
    result = _runner(tmp_path).run_argv([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3
    assert not result.ok


def test_run_argv_missing_executable(tmp_path):
    result = _runner(tmp_path).run_argv(["definitely-not-a-real-bin-xyz"])
    assert result.launch_error is not None


def test_nul_bytes_rejected(tmp_path):
    result = _runner(tmp_path).run_argv(["echo", "a\x00b"])
    assert result.launch_error is not None


def test_invalid_env_names_dropped(tmp_path):
    result = _runner(tmp_path).run_argv(
        [sys.executable, "-c", "import os; print('BAD=NAME' in os.environ)"],
        env_extra={"BAD=NAME": "1", "GOOD_VAR": "ok"},
    )
    assert "False" in result.stdout


def test_run_script_bash(tmp_path):
    result, path = _runner(tmp_path).run_script(
        "echo one\necho two >&2\nexit 0", cwd=tmp_path, shell="bash"
    )
    assert result.ok
    assert "one" in result.stdout
    assert path is None  # script cleaned up


def test_run_script_failure_keeps_stdout(tmp_path):
    result, _ = _runner(tmp_path).run_script(
        "echo before failure\nexit 7", cwd=tmp_path, shell="bash"
    )
    assert result.returncode == 7
    assert "before failure" in result.stdout


def test_run_script_cwd_respected(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    result, _ = _runner(tmp_path).run_script("test -f marker.txt", cwd=tmp_path, shell="bash")
    assert result.ok


def test_unsupported_shell(tmp_path):
    result, _ = _runner(tmp_path).run_script("do stuff", cwd=tmp_path, shell="zsh-bogus")
    assert result.launch_error is not None


def test_timeout_reported(tmp_path):
    result = _runner(tmp_path).run_argv(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1
    )
    assert result.timed_out
