"""Shared test fixtures: offline git repos, fake GitHub, isolated streams.

The whole suite runs offline — GitHub interactions go through FakeGitHub.
SecretShield's global stream hook is disabled for tests (its adapter logic is
still exercised directly), and the CLI's stream hook is neutralized so pytest
capture works.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest
import secretshield

# SecretShield installs a stdout/stderr wrapper at import time. Neutralize it
# *during collection*, before pytest swaps in its capture streams, so capsys
# keeps working. Sanitizer logic is still exercised directly in unit tests.
with suppress(Exception):
    secretshield.disable()
with suppress(Exception):
    secretshield.configure(notify=False)


@pytest.fixture(autouse=True)
def _neutralize_stream_hooks(monkeypatch):
    """Keep pytest capture working; the CLI's global stream hook is a no-op."""
    monkeypatch.setattr("replico.cli.enable_global_protection", lambda sanitizer: None)
    yield


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=60, check=False)


def make_git_repo(
    tmp_path: Path,
    *,
    name: str = "demo",
    owner: str = "octocat",
    files: dict[str, str] | None = None,
    origin: str | None = None,
) -> tuple[Path, str]:
    """Create a git repository with an origin remote; return (root, head_sha)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    result = _git(["init", "-b", "main"], root)
    assert result.returncode == 0, result.stderr
    for filename, content in (files or {"README.md": "# demo\n"}).items():
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Replico Test"], root)
    commit = _git(["commit", "-m", "initial"], root)
    assert commit.returncode == 0, commit.stderr
    origin = origin or f"https://github.com/{owner}/{name}.git"
    remote = _git(["remote", "add", "origin", origin], root)
    assert remote.returncode == 0, remote.stderr
    sha = _git(["rev-parse", "HEAD"], root).stdout.decode().strip()
    return root, sha


def host_family() -> str:
    system = sys.platform
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def runner_for_host() -> str:
    return {
        "windows": "windows-2022",
        "linux": "ubuntu-24.04",
        "macos": "macos-14",
    }[host_family()]


class FakeGitHub:
    """Offline stand-in for replico.github.client.GitHubClient."""

    def __init__(
        self,
        *,
        run_id: int = 123456789,
        owner: str = "octocat",
        repo: str = "demo",
        workflow_name: str = "Tests",
        workflow_id: int = 42,
        head_sha: str = "",
        head_branch: str = "main",
        event: str = "push",
        conclusion: str = "failure",
        jobs: list[dict] | None = None,
        workflow_yaml: str = "",
        workflow_path: str = ".github/workflows/ci.yml",
        job_log: str = "",
    ) -> None:
        self.run_id = run_id
        self.owner = owner
        self.repo = repo
        self.workflow_name = workflow_name
        self.workflow_id = workflow_id
        self.head_sha = head_sha or "a" * 40
        self.head_branch = head_branch
        self.event = event
        self.conclusion = conclusion
        self.jobs = jobs or []
        self.workflow_yaml = workflow_yaml
        self.workflow_path = workflow_path
        self.job_log = job_log
        self.calls: list[str] = []

    # -- client API ----------------------------------------------------------

    def get_run(self, ref):
        from replico.models import RunInfo

        self.calls.append(f"get_run:{ref.run_id}")
        return RunInfo(
            id=ref.run_id,
            owner=ref.owner,
            repo=ref.repo,
            workflow_id=self.workflow_id,
            workflow_name=self.workflow_name,
            head_sha=self.head_sha,
            head_branch=self.head_branch,
            event=self.event,
            status="completed",
            conclusion=self.conclusion,
            html_url=ref.html_url,
        )

    def get_jobs(self, ref, run_id: int | None = None):
        from replico.models import JobInfo, StepRunInfo

        self.calls.append(f"get_jobs:{ref.run_id}")
        jobs = []
        for job in self.jobs:
            steps = [
                StepRunInfo(
                    number=int(s["number"]),
                    name=s["name"],
                    conclusion=s.get("conclusion"),
                    status=s.get("status"),
                )
                for s in job.get("steps", [])
            ]
            jobs.append(
                JobInfo(
                    id=int(job.get("id", 0)),
                    name=job["name"],
                    conclusion=job.get("conclusion"),
                    status=job.get("status"),
                    steps=steps,
                )
            )
        return jobs

    def get_workflow_path(self, ref, workflow_id: int) -> str:
        self.calls.append("get_workflow_path")
        return self.workflow_path

    def get_file_content(self, ref, path: str, ref_sha: str) -> str:
        self.calls.append(f"get_file_content:{path}")
        return self.workflow_yaml

    def get_job_logs(self, ref, job_id: int) -> str:
        self.calls.append(f"get_job_logs:{job_id}")
        return self.job_log

    def _effective_token(self) -> str | None:
        return None

    @property
    def api_base(self) -> str:
        return "https://api.github.com"


def standard_failing_jobs(*, job_name: str = "test", failing_step: str = "Run tests") -> list[dict]:
    return [
        {
            "id": 1,
            "name": job_name,
            "conclusion": "failure",
            "status": "completed",
            "steps": [
                {"number": 1, "name": "Checkout", "conclusion": "success", "status": "completed"},
                {
                    "number": 2,
                    "name": "Set up Python",
                    "conclusion": "success",
                    "status": "completed",
                },
                {"number": 3, "name": failing_step, "conclusion": "failure", "status": "completed"},
            ],
        }
    ]


def python_workflow(
    *, runner: str | None = None, extra_run: str = "", shell: str | None = "bash"
) -> str:
    """A minimal python job. Failing step runs under `shell` (bash by default
    so the same YAML reproduces on every host OS)."""
    runner = runner or runner_for_host()
    shell_line = f"        shell: {shell}\n" if shell else ""
    return f"""name: Tests
on: [push]

jobs:
  test:
    runs-on: {runner}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3'
      - name: Run tests
{shell_line}        run: |
          echo "running the test suite"
          {extra_run}
"""


def failing_log(test_id: str = "tests/test_auth.py::test_login") -> str:
    return f"""##[group]Run actions/checkout@v4
##[endgroup]
##[group]Run tests
============================= test session starts =============================
collected 3 items

tests/test_auth.py::test_login FAILED
tests/test_auth.py::test_signup PASSED

---------- FAILURES ----------
____ test_login ____
tests/test_auth.py:12: in test_login
    assert response.status_code == 200
E   AssertionError: expected 200, received 401

FAILED {test_id} - AssertionError: expected 200, received 401
short test summary info
FAILED {test_id} - AssertionError: expected 200, received 401
Error: Process completed with exit code 1.
##[endgroup]
"""
