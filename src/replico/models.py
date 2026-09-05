"""Shared data models used across Replico's layers.

Kept deliberately small and dependency-free (plain dataclasses) so the
analysis, planning and reporting layers can agree on one vocabulary without
importing UI or network code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    """Final outcome of a reproduction attempt.

    Never claim more than the evidence supports:

    * REPRODUCED — the failing command also failed locally and the failure
      signature (failing test id and/or primary error) matches CI.
    * PARTIALLY_REPRODUCED — a failure occurred locally but its identity
      could not be confirmed against CI, or material environment
      differences prevent a confident claim.
    * NOT_REPRODUCED — the failing command passed locally under adequate
      environment parity.
    * UNSUPPORTED — Replico does not know how to reproduce this workflow.
    """

    REPRODUCED = "reproduced"
    PARTIALLY_REPRODUCED = "partially_reproduced"
    NOT_REPRODUCED = "not_reproduced"
    UNSUPPORTED = "unsupported"


@dataclass
class RunInfo:
    """A workflow run as reported by the GitHub API (kept minimal)."""

    id: int
    owner: str
    repo: str
    workflow_id: int
    workflow_name: str
    head_sha: str
    head_branch: str | None
    event: str
    status: str
    conclusion: str | None
    html_url: str | None = None
    display_title: str | None = None


@dataclass
class StepRunInfo:
    """One executed step inside a job run (from the jobs API)."""

    number: int
    name: str
    conclusion: str | None = None
    status: str | None = None


@dataclass
class JobInfo:
    """A job execution as reported by the GitHub API."""

    id: int
    name: str
    conclusion: str | None = None
    status: str | None = None
    steps: list[StepRunInfo] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class WorkflowStep:
    """A step from the workflow YAML."""

    number: int
    name: str | None
    uses: str | None
    run: str | None
    with_args: dict = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    working_directory: str | None = None
    shell: str | None = None
    step_id: str | None = None
    if_condition: str | None = None

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.uses:
            return f"Run {self.uses}"
        if self.run:
            first = self.run.strip().splitlines()[0].strip() if self.run.strip() else ""
            return f"Run {first[:60]}" if first else "Run"
        return f"Step {self.number}"


@dataclass
class MatrixSpec:
    """Raw strategy.matrix information (best effort)."""

    axes: list[tuple[str, list]] = field(default_factory=list)  # key -> values
    include: list[dict] = field(default_factory=list)
    exclude: list[dict] = field(default_factory=list)
    name_template: str | None = None


@dataclass
class WorkflowJob:
    """A job definition from the workflow YAML."""

    id: str
    name: str | None
    runs_on: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)
    matrix: MatrixSpec | None = None
    default_shell: str | None = None
    needs: list[str] = field(default_factory=list)
    timeout_minutes: int | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class Workflow:
    """A parsed GitHub Actions workflow file."""

    path: str
    name: str | None
    jobs: dict[str, WorkflowJob]
    raw_text: str = ""  # original YAML text; redact before persisting

    @property
    def display_name(self) -> str:
        return self.name or self.path


@dataclass
class FailureEvidence:
    """Condensed, evidence-backed summary of why CI failed."""

    summary: str = ""
    category_hint: str = ""
    failing_tests: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # sanitized relevant lines
    line_numbers: list[int] = field(default_factory=list)
    source: str = ""  # human readable: "GitHub Actions → job → step"
    exit_code: int | None = None
