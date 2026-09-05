"""Tests for job analysis: setup detection, ecosystems, installs, rendering."""

from __future__ import annotations

from replico.workflow.detector import (
    analyze_job,
    github_context_vars,
    render_expressions,
)
from replico.workflow.matcher import JobMatch, match_api_job, matrix_combinations
from replico.workflow.parser import parse_workflow

WORKFLOW = """name: Tests
jobs:
  test:
    name: Python Tests
    runs-on: ubuntu-24.04
    env:
      CI_MODE: nightly
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - name: Install deps
        run: |
          pip install -r requirements.txt
          pip install -e .
      - name: Run tests
        run: pytest tests/ -q
        env:
          REPORT_TOKEN: ${{ secrets.REPORT_TOKEN }}
  node:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci
      - run: npm test
"""


def _job(name="test"):
    return parse_workflow(WORKFLOW).jobs[name]


def test_python_job_analysis():
    analysis = analyze_job(_job(), github=github_context_vars_from_run())
    assert analysis.runner_image == "ubuntu-24.04"
    assert analysis.runner_os == "linux"
    assert analysis.referenced_python_version == "3.13"
    assert "python" in analysis.ecosystems
    assert "checkout" in analysis.setup_items
    installs = [cmd.kind for cmd in analysis.install_commands]
    assert installs == ["pip", "pip"]
    assert "REPORT_TOKEN" in analysis.secret_names or (
        any("secrets.REPORT_TOKEN" in v for v in analysis.merged_env.values())
    )
    assert analysis.merged_env.get("CI_MODE") == "nightly"


def github_context_vars_from_run(**overrides):
    class _Run:
        id = 123
        owner = "o"
        repo = "r"
        head_sha = "a" * 40
        head_branch = "main"
        event = "push"
        workflow_name = "Tests"

    run = _Run()
    for key, value in overrides.items():
        setattr(run, key, value)
    return github_context_vars(run)  # type: ignore[arg-type]


def test_node_job_detected():
    analysis = analyze_job(_job("node"), github=github_context_vars_from_run())
    assert "node" in analysis.ecosystems
    assert analysis.referenced_node_version == "20"
    # only `npm ci` is an install; `npm test` is not
    kinds = [cmd.kind for cmd in analysis.install_commands]
    assert kinds == ["npm"]
    assert analysis.install_commands[0].command == "npm ci"


def test_secret_expressions_render_to_marker():
    rendered, secrets, unresolved = render_expressions(
        "curl -H 'Authorization: Bearer ${{ secrets.TOKEN }}' https://x",
        combo={},
        github={},
        env={},
    )
    assert secrets == {"TOKEN"}
    assert "__REPLICO_SECRET_UNAVAILABLE__" in rendered
    assert unresolved == []


def test_matrix_expressions_render():
    rendered, _secrets, unresolved = render_expressions(
        "echo running on ${{ matrix.os }} with python ${{ matrix.python }}",
        combo={"os": "ubuntu-latest", "python": "3.13"},
        github={},
        env={},
    )
    assert rendered == "echo running on ubuntu-latest with python 3.13"
    assert unresolved == []


def test_github_expressions_render():
    ctx = github_context_vars_from_run(event="pull_request")
    rendered, _s, _u = render_expressions(
        "echo repo=${{ github.repository }} event=${{ github.event_name }}",
        combo={},
        github=ctx,
        env={},
    )
    assert rendered == "echo repo=o/r event=pull_request"


def test_matrix_combinations_and_matching():
    workflow = parse_workflow(
        """jobs:
  matrix-job:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ['3.11', '3.12']
    steps:
      - run: echo hi
"""
    )
    job = workflow.jobs["matrix-job"]
    combos = matrix_combinations(job)
    assert len(combos) == 4
    # GitHub names matrix jobs like "matrix-job (ubuntu-latest, 3.11)"
    matched = match_api_job(workflow, "matrix-job (ubuntu-latest, 3.11)")
    assert matched is not None
    assert matched.combo == {"os": "ubuntu-latest", "python": "3.11"}


def test_match_by_plain_job_id():
    workflow = parse_workflow(WORKFLOW)
    match = match_api_job(workflow, "test")
    assert isinstance(match, JobMatch)
    assert match.job.id == "test"
    assert match_api_job(workflow, "does-not-exist") is None


def test_include_adds_combo():
    workflow = parse_workflow(
        """jobs:
  j:
    strategy:
      matrix:
        python: ['3.11']
        include:
          - python: '3.12'
            experimental: true
    steps:
      - run: echo ${{ matrix.python }}
"""
    )
    combos = matrix_combinations(workflow.jobs["j"])
    assert {"python": "3.12", "experimental": "true"} in combos
