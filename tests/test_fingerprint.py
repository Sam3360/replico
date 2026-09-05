"""Environment fingerprint / parity tests."""

from __future__ import annotations

from replico.environments.fingerprint import (
    LocalEnvironment,
    capture_local_environment,
    compare_environments,
)
from replico.workflow.parser import parse_workflow

WF = """jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install -r requirements.txt
      - run: python -m pytest
"""


def _analysis():
    from replico.workflow.detector import analyze_job

    job = parse_workflow(WF).jobs["test"]
    return analyze_job(job, github={})


def _local(**overrides):
    base = dict(
        os_label="Windows 10/11",
        os_family="windows",
        arch="x64",
        python_version="3.13.1",
        python_path="C:/x/python.exe",
        git_version="git version 2.45.0",
        node_version=None,
        docker_available=False,
        cwd="C:/repo",
    )
    base.update(overrides)
    return LocalEnvironment(**base)


def test_matching_env_high_parity():
    analysis = _analysis()
    analysis.referenced_python_version = "3.13"
    local = _local()
    diffs, parity = compare_environments(
        local, analysis, docker=False, python_override="3.13.1", deps_ok=True, isolated=True
    )
    assert isinstance(parity, int)
    # OS mismatches (ubuntu vs windows) cost heavily:
    assert parity < 100
    assert any(diff.ok is False and "OS" in diff.label for diff in diffs)


def test_docker_assumed_matching():
    analysis = _analysis()
    local = _local()
    diffs, parity = compare_environments(
        local, analysis, docker=True, python_override="3.13.2", deps_ok=True
    )
    assert parity >= 90
    assert all(diff.ok is not False for diff in diffs)


def test_python_version_mismatch_lowers_parity():
    analysis = _analysis()  # requested 3.13
    diffs, parity = compare_environments(
        _local(python_version="3.12.4"),
        analysis,
        docker=True,
        python_override="3.12.4",
        deps_ok=True,
    )
    assert parity <= 80
    assert any(diff.ok is False and "Python" in diff.label for diff in diffs)


def test_missing_deps_flag():
    analysis = _analysis()
    diffs, parity = compare_environments(
        _local(), analysis, docker=True, python_override="3.13.1", deps_ok=False
    )
    assert parity <= 90
    assert any(diff.ok is False and "dependencies" in diff.label for diff in diffs)


def test_capture_local_has_safe_shape(monkeypatch):
    local = capture_local_environment()
    mapping = local.to_mapping()
    assert mapping["os"]
    assert isinstance(mapping["environment_variables"], list)
    assert all(isinstance(name, str) for name in mapping["environment_variables"])
    assert "python_version" in mapping
