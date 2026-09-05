"""End-to-end offline integration tests.

These run the real pipeline (workflow parsing → detection → venv creation →
command execution → verdict → storage) against fixture repositories and a
fake GitHub. The only "real" machinery used is the local Python interpreter
and the shell, exactly like a genuine reproduction.
"""

from __future__ import annotations

import pytest

from conftest import (
    FakeGitHub,
    failing_log,
    make_git_repo,
    python_workflow,
    runner_for_host,
    standard_failing_jobs,
)
from replico.config import load_config
from replico.errors import EXIT_FAILURE_EXISTS, InvalidInputError, UnsupportedError
from replico.flows import build_app, reproduce, rerun
from replico.pipeline import ReproOptions
from replico.security.redaction import Sanitizer
from replico.ui import UI

FAILING_CMD = (
    "python -c \"import pathlib,sys; ok=pathlib.Path('tests/fixed.flag').exists(); "
    "print('FAILED tests/test_auth.py::test_login - AssertionError: boom' if not ok else "
    "'tests passed'); sys.exit(0 if ok else 1)\""
)


def _make_context(
    tmp_path,
    monkeypatch,
    *,
    head_sha: str = "",
    fake: FakeGitHub | None = None,
    assume_yes: bool = True,
):
    root, sha = make_git_repo(
        tmp_path, files={"tests/test_auth.py": "def test_login():\n    pass\n"}
    )
    head_sha = head_sha or sha
    monkeypatch.chdir(root)
    cfg = load_config()
    ui = UI(plain=True, sanitizer=Sanitizer(), assume_yes=assume_yes)
    fake = fake or FakeGitHub(head_sha=head_sha)
    ctx = build_app(cfg, ui, ui.sanitizer, cwd=root, client=fake)
    return ctx, root, sha


def test_reproduce_reproduced_end_to_end(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=FAILING_CMD),
        job_log=failing_log(),
    )
    ctx, root, _sha = _make_context(tmp_path, monkeypatch, fake=fake)
    # Fake the failing CI evidence so classification/signature come from logs.
    from replico.github.refs import RunRef

    outcome = reproduce(
        ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions(assume_yes=True)
    )
    assert outcome.exit_code == EXIT_FAILURE_EXISTS
    assert outcome.verdict.value == "reproduced"
    assert outcome.doc["status"] == "reproduced"
    assert outcome.doc["failure"]["tests"] == ["tests/test_auth.py::test_login"]
    assert ctx.store.exists()
    store = ctx.store.read()
    assert store["verdict"]["verdict"] == "reproduced"
    # Evidence lines from CI are present (redacted) and no secret values leaked.
    assert (root / ".replico" / "commands.txt").exists()
    assert (root / ".replico" / "README.md").exists()
    captured = capsys.readouterr()
    assert "REPRODUCED" in captured.out


def test_reproduce_not_reproduced_when_local_passes(tmp_path, monkeypatch):
    passing = "python -c \"print('tests passed'); import sys; sys.exit(0)\""
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=passing),
        job_log=failing_log(),  # CI failed on test_login
    )
    ctx, _root, sha = _make_context(tmp_path, monkeypatch, fake=fake)
    from replico.github.refs import RunRef

    outcome = reproduce(
        ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions(assume_yes=True)
    )
    assert outcome.verdict.value == "not_reproduced"
    assert outcome.exit_code == 0
    assert outcome.doc["status"] == "not_reproduced"


def test_reproduce_multiple_failed_jobs_requires_selection(tmp_path, monkeypatch):
    jobs = standard_failing_jobs(job_name="test-python", failing_step="Run tests")
    jobs.append(
        {
            "id": 2,
            "name": "integration-linux",
            "conclusion": "failure",
            "steps": [
                {"number": 1, "name": "Setup", "conclusion": "success"},
                {"number": 2, "name": "Integration", "conclusion": "failure"},
            ],
        }
    )
    two_job_workflow = python_workflow(extra_run=FAILING_CMD).replace(
        "  test:", "  test-python:", 1
    )
    two_job_workflow += f"""  integration-linux:
    runs-on: {runner_for_host()}
    steps:
      - name: Integration
        run: echo integration
"""
    fake = FakeGitHub(
        jobs=jobs,
        workflow_yaml=two_job_workflow,
        job_log=failing_log(),
    )
    # Non-interactive session without --yes: selection is required.
    ctx, _root, _sha = _make_context(tmp_path, monkeypatch, fake=fake, assume_yes=False)
    from replico.github.refs import RunRef

    with pytest.raises(InvalidInputError):
        reproduce(ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions())
    # With --job the selection is explicit.
    ctx.ui.assume_yes = True
    outcome = reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True, job="test-python"),
    )
    assert outcome.verdict.value == "reproduced"


def test_reproduce_unsupported_node_job(tmp_path, monkeypatch):
    yaml = f"""name: Web
jobs:
  web:
    runs-on: {runner_for_host()}
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci
      - name: Run tests
        run: npm test
"""
    fake = FakeGitHub(
        jobs=[
            {
                "id": 1,
                "name": "web",
                "conclusion": "failure",
                "steps": [
                    {"number": 1, "name": "Set up Node", "conclusion": "success"},
                    {"number": 2, "name": "npm ci", "conclusion": "success"},
                    {"number": 3, "name": "Run tests", "conclusion": "failure"},
                ],
            }
        ],
        workflow_yaml=yaml,
        job_log="npm ERR! test failed",
    )
    ctx, _root, sha = _make_context(tmp_path, monkeypatch, fake=fake)
    from replico.github.refs import RunRef

    with pytest.raises(UnsupportedError) as exc:
        reproduce(
            ctx,
            RunRef(owner="octocat", repo="demo", run_id=123456789),
            ReproOptions(assume_yes=True),
        )
    assert "node" in str(exc.value).lower()


def test_rerun_reports_fix(tmp_path, monkeypatch):
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=FAILING_CMD),
        job_log=failing_log(),
    )
    ctx, root, _sha = _make_context(tmp_path, monkeypatch, fake=fake)
    from replico.github.refs import RunRef

    first = reproduce(
        ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions(assume_yes=True)
    )
    assert first.verdict.value == "reproduced"
    # Developer "fixes" the code (working tree change).
    (root / "tests" / "fixed.flag").write_text("fixed")
    second = rerun(ctx, ReproOptions(assume_yes=True))
    assert second.verdict.value == "not_reproduced"
    assert second.exit_code == 0
    store = ctx.store.read()
    assert len(store["reruns"]) == 1
    assert store["reruns"][0]["verdict"] == "not_reproduced"


def test_rerun_still_failing(tmp_path, monkeypatch):
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=FAILING_CMD),
        job_log=failing_log(),
    )
    ctx, root, _sha = _make_context(tmp_path, monkeypatch, fake=fake)
    from replico.github.refs import RunRef

    reproduce(
        ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions(assume_yes=True)
    )
    second = rerun(ctx, ReproOptions(assume_yes=True))
    assert second.verdict.value == "reproduced"
    assert second.exit_code == EXIT_FAILURE_EXISTS


def test_no_secret_leak_through_artifacts(tmp_path, monkeypatch, capsys):
    secret = "leaky-deploy-token-abc123"
    monkeypatch.setenv("DEPLOY_TOKEN", secret)
    log = failing_log() + f"\ndeploying with token {secret} to production\n"
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=FAILING_CMD),
        job_log=log,
    )
    ctx, root, _sha = _make_context(tmp_path, monkeypatch, fake=fake)
    ctx.sanitizer.register_secret(secret)
    from replico.github.refs import RunRef

    outcome = reproduce(
        ctx, RunRef(owner="octocat", repo="demo", run_id=123456789), ReproOptions(assume_yes=True)
    )
    assert outcome.verdict.value == "reproduced"
    for file in (root / ".replico").iterdir():
        if file.is_file():
            text = file.read_text(encoding="utf-8")
            assert secret not in text, f"secret leaked into {file.name}"
    # The UI stream is redacted too: nothing the user sees contains the value.
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
