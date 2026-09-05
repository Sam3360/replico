"""Match an executed GitHub job (from the API) to a workflow YAML job.

Matrix jobs complicate this: the API reports each matrix combination as a
separate job whose name looks like ``test (3.12, ubuntu-latest)``. We try the
id, the explicit ``name:`` and several plausible expansions of the matrix.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from replico.models import Workflow, WorkflowJob


@dataclass
class JobMatch:
    job: WorkflowJob
    combo: dict[str, str] | None = None  # resolved matrix variables
    matched_by: str = ""


def matrix_combinations(job: WorkflowJob) -> list[dict[str, str]]:
    """Cartesian product of matrix axes, with include/exclude applied.

    Best-effort and conservative: axes are combined in declaration order,
    ``exclude`` entries are dropped, and ``include`` entries are appended
    when they add a full set of variables. Returns at least ``[{}]``.
    """
    if job.matrix is None or not job.matrix.axes:
        return [{}]
    axis_values = [values for _key, values in job.matrix.axes]
    combos: list[dict[str, str]] = []
    for product in itertools.product(*axis_values):
        combo = {job.matrix.axes[i][0]: product[i] for i in range(len(job.matrix.axes))}
        combos.append(combo)
    # include entries that fully specify a combination get their own combo.
    for entry in job.matrix.include:
        combos.append(dict(entry))
    # apply excludes (simple equality on the subset of keys)
    filtered: list[dict[str, str]] = []
    for combo in combos:
        excluded = any(
            all(combo.get(key) == value for key, value in exclude.items())
            for exclude in job.matrix.exclude
        )
        if not excluded:
            filtered.append(combo)
    return filtered or [{}]


def _default_display_names(job: WorkflowJob, combo: dict[str, str]) -> list[str]:
    axis_keys = [key for key, _values in job.matrix.axes] if job.matrix else []
    values = [combo.get(key, "") for key in axis_keys]
    extras = [v for key, v in combo.items() if key not in axis_keys]
    names: list[str] = []
    for ordered_values in (values + extras, sorted(values + extras)):
        joined = ", ".join(ordered_values)
        names.append(f"{job.id} ({joined})")
        if job.name:
            names.append(f"{job.name} ({joined})")
    return list(dict.fromkeys(names))


def _render_name_template(template: str, combo: dict[str, str]) -> str:
    import re

    def repl(match: re.Match) -> str:
        expr = match.group(1).strip()
        if expr.startswith("matrix."):
            key = expr[len("matrix.") :].strip()
            return combo.get(key, match.group(0))
        return match.group(0)

    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", repl, template)


def match_api_job(workflow: Workflow, api_job_name: str) -> JobMatch | None:
    """Find the YAML job that produced an executed job with the given name."""
    name = api_job_name.strip()
    # 1) exact job id / explicit name, no matrix expansion needed
    for job_id, job in workflow.jobs.items():
        if job_id == name or (job.name and job.name == name):
            return JobMatch(job=job, matched_by="job id/name")
    # 2) matrix-expanded display names
    for job in workflow.jobs.values():
        if job.matrix is None:
            continue
        for combo in matrix_combinations(job):
            if job.matrix.name_template:
                rendered = _render_name_template(job.matrix.name_template, combo)
                if rendered == name:
                    return JobMatch(job=job, combo=combo, matched_by="matrix name template")
            for candidate in _default_display_names(job, combo):
                if candidate == name:
                    return JobMatch(job=job, combo=combo, matched_by="matrix display name")
    return None


def find_job_by_id(workflow: Workflow, job_id: str) -> WorkflowJob | None:
    return workflow.jobs.get(job_id)
