"""Reproduction engine: GitHub run → analysis → plan → execute → verdict.

This module orchestrates the end-to-end reproduce/rerun flows. Everything it
prints is routed through the UI (which sanitizes), everything it persists
goes through the store (which sanitizes), and every command it executes goes
through the audited Runner / DockerExecutor.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from replico.analysis import classifier as clsf
from replico.analysis.logs import analyze_log
from replico.config import Config
from replico.environments.fingerprint import (
    Difference,
    LocalEnvironment,
    capture_local_environment,
    compare_environments,
)
from replico.environments.python import PythonRuntime, ensure_venv, find_python
from replico.errors import (
    AuthError,
    InvalidInputError,
    ReproNotPossibleError,
    SetupError,
    UnsupportedError,
)
from replico.execution.docker import DockerExecutor, docker_info, image_for, require_docker
from replico.execution.runner import ExecResult, Runner
from replico.github.client import GitHubClient, NotFoundError
from replico.github.refs import RunRef
from replico.gitrepo import GitRepo, matches_run_repo
from replico.models import (
    FailureEvidence,
    JobInfo,
    RunInfo,
    StepRunInfo,
    Verdict,
    WorkflowJob,
    WorkflowStep,
)
from replico.security.guard import audit_command_text
from replico.security.redaction import Sanitizer
from replico.storage.store import ReproductionStore
from replico.ui import UI, humanize_duration
from replico.workflow.detector import (
    JobAnalysis,
    analyze_job,
    github_context_vars,
    render_expressions,
)

PARITY_THRESHOLD = 75


@dataclass
class ReproOptions:
    job: str | None = None
    step: str | None = None
    docker: bool | None = None
    offline: bool = False
    keep: bool = False
    clean_first: bool = False
    assume_yes: bool = False
    # None = auto (WhyFail runs for eligible reproduced Python failures);
    # True forces diagnosis; False disables it entirely.
    diagnose: bool | None = None


@dataclass
class AppContext:
    cfg: Config
    ui: UI
    sanitizer: Sanitizer
    cwd: Path
    repo: GitRepo | None
    store: ReproductionStore
    client: GitHubClient | None = None
    runner: Runner = field(default_factory=Runner)

    @property
    def repo_root(self) -> Path:
        return self.repo.root if self.repo else self.cwd


@dataclass
class Outcome:
    exit_code: int
    verdict: Verdict | None
    doc: dict
    headline: str = ""


def find_venv_dir(ctx: AppContext) -> Path:
    """Venv must live inside .replico/ so `replico clean` removes it too."""
    venv_rel = ctx.cfg.python.venv_dir or ".replico/venv"
    venv = Path(venv_rel)
    if not venv.is_absolute():
        venv = ctx.repo_root / venv_rel
    venv = venv.resolve()
    store_dir = (ctx.repo_root / ".replico").resolve()
    try:
        venv.relative_to(store_dir)
    except ValueError:
        raise SetupError(f"python.venv_dir must live inside .replico/ (got {venv_rel!r})") from None
    return venv


def _outcome_for_verdict(verdict: Verdict) -> int:
    from replico.errors import (
        EXIT_COULD_NOT_REPRODUCE,
        EXIT_FAILURE_EXISTS,
        EXIT_OK,
        EXIT_UNSUPPORTED,
    )

    return {
        Verdict.REPRODUCED: EXIT_FAILURE_EXISTS,
        Verdict.NOT_REPRODUCED: EXIT_OK,
        Verdict.PARTIALLY_REPRODUCED: EXIT_COULD_NOT_REPRODUCE,
        Verdict.UNSUPPORTED: EXIT_UNSUPPORTED,
    }[verdict]


# --------------------------------------------------------------------------
# Step 1 — GitHub resolution
# --------------------------------------------------------------------------


def _resolve_run(
    ctx: AppContext, ref: RunRef | None, options: ReproOptions
) -> tuple[RunInfo, GitHubClient | None]:
    if ref is None:
        raise InvalidInputError("a GitHub Actions run URL or numeric run id is required")
    if options.offline:
        payload = ctx.store.read()
        saved = payload.get("run") or {}
        if saved.get("run_id") is None:
            raise InvalidInputError("saved reproduction has no run metadata for offline use")
        if saved and int(saved["run_id"]) != ref.run_id:
            raise InvalidInputError(
                f"saved reproduction is for run #{saved.get('run_id')}, not #{ref.run_id}"
            )
        run = RunInfo(
            id=ref.run_id,
            owner=str(saved.get("owner") or ref.owner),
            repo=str(saved.get("repo") or ref.repo),
            workflow_id=int(saved.get("workflow_id") or 0),
            workflow_name=str(saved.get("workflow_name") or ""),
            head_sha=str(saved.get("head_sha") or ""),
            head_branch=saved.get("head_branch"),
            event=str(saved.get("event") or ""),
            status="completed",
            conclusion=str(saved.get("conclusion") or "failure"),
            html_url=saved.get("html_url"),
        )
        return run, None

    client = ctx.client or GitHubClient(
        api_base=ctx.cfg.github.api_base,
        timeout_s=ctx.cfg.github.timeout_s,
        log_max_bytes=ctx.cfg.github.log_max_bytes,
    )
    try:
        token = client._effective_token()
        if token:
            ctx.sanitizer.register_secret(token)
    except Exception:  # noqa: BLE001 - token registration must never break the flow
        pass
    ctx.ui.status(f"fetching {ref.owner}/{ref.repo} run #{ref.run_id}…")
    try:
        run = client.get_run(ref)
    except NotFoundError:
        raise InvalidInputError(
            f"GitHub reports run {ref.html_url} as not found — check the URL; for "
            "private repositories set GITHUB_TOKEN or run `gh auth login`"
        ) from None
    if run.status in ("queued", "in_progress", "waiting", "requested", "pending"):
        raise InvalidInputError(f"run #{run.id} is still {run.status} — wait for it to finish")
    if run.conclusion not in ("failure", "timed_out", "startup_failure"):
        raise InvalidInputError(
            f"run #{run.id} concluded as {run.conclusion!r} — there is no failure to reproduce"
        )
    return run, client


def _require_local_repo(ctx: AppContext, owner: str, repo: str) -> None:
    """We reproduce inside the project checkout; verify before any network I/O."""
    if ctx.repo is None:
        raise InvalidInputError(
            "Replico reproduces CI runs inside the repository checkout that failed. "
            "Run this command from a git checkout of the project.",
            hint=f"cd /path/to/{repo} && replico <run-url>",
        )
    if not matches_run_repo(ctx.repo, owner, repo):
        raise InvalidInputError(
            f"the local repository {ctx.repo.full_name or '(no origin remote)'} does not "
            f"match the failed repository {owner}/{repo} — run Replico inside "
            "a checkout of that repository (a fork with the same name is fine)"
        )


def _select_failed_job(
    ctx: AppContext, run: RunInfo, jobs: list[JobInfo], options: ReproOptions
) -> JobInfo:
    failed = [job for job in jobs if job.conclusion == "failure"]
    if not failed:
        conclusions = ", ".join(sorted({job.conclusion or "?" for job in jobs})) or "none"
        raise InvalidInputError(f"no failed job in run #{run.id} (job conclusions: {conclusions})")
    if options.job:
        wanted = options.job.lower()
        matches = [job for job in failed if wanted in job.name.lower()]
        if not matches:
            raise InvalidInputError(
                f"--job {options.job!r} matched none of the failed jobs: "
                + ", ".join(job.name for job in failed)
            )
        if len(matches) > 1:
            raise InvalidInputError(
                f"--job {options.job!r} is ambiguous (matches "
                + ", ".join(job.name for job in matches)
                + ")"
            )
        return matches[0]
    if len(failed) == 1:
        return failed[0]

    ctx.ui.warn(f"{len(failed)} failed jobs detected:")
    for index, job in enumerate(failed, start=1):
        ctx.ui.out(f"  {index}. {job.name}")
    if ctx.ui.confirm("Reproduce only the first failed job?", default=False):
        return failed[0]
    raise InvalidInputError(
        "multiple jobs failed — pick one with:  replico <run-url> --job <job>",
        hint="example: replico ... --job " + failed[0].name,
    )


def _select_failed_step(job: JobInfo, options: ReproOptions) -> StepRunInfo:
    if options.step:
        wanted = options.step.lower()
        for step in job.steps:
            if wanted in step.name.lower() or str(step.number) == wanted:
                return step
        names = ", ".join(f"{s.number}: {s.name}" for s in job.steps)
        raise InvalidInputError(f"--step {options.step!r} matched none of the steps:\n{names}")
    failed_steps = [step for step in job.steps if step.conclusion == "failure"]
    if not failed_steps:
        raise UnsupportedError(
            f"job {job.name!r} failed without an individual failing step — "
            "Replico cannot reconstruct the failing command"
        )
    return failed_steps[0]


def _find_workflow_step(workflow_job: WorkflowJob, api_step: StepRunInfo) -> WorkflowStep | None:
    """Map an executed step back to its YAML definition."""
    for step in workflow_job.steps:
        if step.name and api_step.name and step.name == api_step.name:
            return step
    index = api_step.number - 1
    if 0 <= index < len(workflow_job.steps):
        candidate = workflow_job.steps[index]
        if not api_step.name.startswith("Post ") and not api_step.name.startswith("Set up"):
            return candidate
    return None


# --------------------------------------------------------------------------
# Steps 2–7 — workflow + analysis
# --------------------------------------------------------------------------


def _analyze(
    ctx: AppContext,
    client: GitHubClient | None,
    ref: RunRef,
    run: RunInfo,
    api_job: JobInfo,
    api_step: StepRunInfo,
    workflow_job: WorkflowJob,
    combo: dict[str, str] | None,
) -> tuple[JobAnalysis, FailureEvidence]:
    github = github_context_vars(run)
    analysis = analyze_job(workflow_job, combo=combo, github=github)

    ctx.ui.status("reading CI logs…")
    log_text = ""
    if client is not None:
        try:
            log_text = client.get_job_logs(ref, api_job.id)
        except NotFoundError:
            ctx.ui.warn("job log no longer available on GitHub (expired?)")
        except SetupError:
            ctx.ui.warn("could not download job logs")
        except AuthError:
            ctx.ui.warn(
                "job logs require a GitHub token — continuing with workflow analysis only "
                "(set GITHUB_TOKEN for log-level failure analysis)"
            )
    if log_text:
        ctx.ui.status("analyzing the failing step…")
    source = f"GitHub Actions → {run.workflow_name or run.id} → {api_job.name} → {api_step.name}"
    evidence = analyze_log(log_text, source=source)
    return analysis, evidence


# --------------------------------------------------------------------------
# Execution plan
# --------------------------------------------------------------------------


def _pick_isolation(
    ctx: AppContext,
    analysis: JobAnalysis,
    options: ReproOptions,
    runtime: PythonRuntime | None,
) -> tuple[bool, str]:
    """Return (use_docker, reason)."""
    requested_mode = (
        ctx.cfg.execution.mode
        if options.docker is None
        else ("docker" if options.docker else "local")
    )
    if requested_mode == "docker":
        return True, "requested via --docker / execution.mode"
    if requested_mode == "local":
        return False, "requested via --no-docker / execution.mode"

    docker_ok = docker_info().available
    local_os = capture_local_environment().os_family
    ci_os = analysis.runner_os
    version_missing = (
        runtime is not None
        and runtime.version is not None
        and analysis.referenced_python_version is not None
        and not runtime.matches_request
    )
    os_mismatch = ci_os != local_os and ci_os in ("linux", "windows", "macos")
    if docker_ok and os_mismatch:
        return True, f"CI runner OS ({analysis.runner_image}) differs from local OS"
    if docker_ok and version_missing:
        return True, f"CI needs Python {analysis.referenced_python_version}, unavailable locally"
    if os_mismatch and not docker_ok:
        return (
            False,
            "CI runner OS differs from local OS and Docker is unavailable — "
            "proceeding locally with reduced parity",
        )
    return False, "local execution (best available match)"


def _plan_execution(
    ctx: AppContext,
    run: RunInfo,
    analysis: JobAnalysis,
    workflow_job: WorkflowJob,
    failed_step: WorkflowStep,
    api_step: StepRunInfo,
    detection,
    use_docker: bool,
    combo: dict[str, str] | None,
) -> dict:
    """Build the concrete execution entries for setup + target."""
    github = github_context_vars(run)
    plan: dict = {
        "use_docker": use_docker,
        "image": None,
        "runtime": None,
        "venv": None,
        "setup_entries": [],
        "target_entries": [],
        "exec_env": {},
        "shell": "bash",
        "shell_note": None,
        "cwd": ctx.repo_root,
        "install_hash": "",
    }
    combo = combo or {}

    if use_docker:
        require_docker()
        plan["image"] = image_for(detection.ecosystem, analysis.referenced_python_version)
    elif detection.ecosystem == "python":
        request = analysis.referenced_python_version or ctx.cfg.python.preferred_version or None
        runtime = find_python(request)
        plan["runtime"] = runtime
        venv_dir = find_venv_dir(ctx)
        ctx.ui.status(
            f"preparing Python {runtime.how}" + (f" (requested {request})" if request else "")
        )
        try:
            venv_py = ensure_venv(venv_dir, runtime)
        except SetupError:
            raise
        plan["venv"] = venv_py
        if runtime.version:
            plan["exec_env"]["REPLICO_PYTHON_VERSION"] = ".".join(str(v) for v in runtime.version)
        plan["exec_env"].update(_venv_path_overlay(ctx, venv_py))
        if request and runtime.version and not runtime.matches_request:
            ctx.ui.warn(
                f"Python {request} was requested in CI but local "
                f"{'.'.join(str(v) for v in runtime.version)} will be used"
            )

    # environment for the failing step
    exec_env = _build_exec_env(ctx, run, analysis, failed_step)
    plan["exec_env"].update(exec_env)
    plan["install_hash"] = _sha256(
        "\n".join(cmd.command for cmd in analysis.install_commands)
        + _requirements_fingerprint(ctx.repo_root)
    )

    # setup entries: dependency installation
    if analysis.install_commands:
        for cmd in analysis.install_commands:
            command = (
                _strip_sudo(cmd.command)
                if use_docker or detection.ecosystem == "python"
                else cmd.command
            )
            if use_docker:
                command = _containerize(command)
            cwd = ctx.repo_root
            if cmd.cwd:
                cwd = ctx.repo_root / cmd.cwd
            plan["setup_entries"].append(
                {
                    "comment": f"install ({cmd.kind}): {cmd.command[:70]}",
                    "kind": "script",
                    "text": command,
                    "shell": "bash",
                    "cwd": cwd,
                    "timeout": ctx.cfg.execution.install_timeout_s,
                }
            )

    # target entry: the failing step
    shell, shell_note = pick_shell(
        failed_step.shell, workflow_job.default_shell, _runner_os(analysis), use_docker
    )
    plan["shell"] = shell or "bash"
    plan["shell_note"] = shell_note
    if shell is None:
        raise ReproNotPossibleError(
            shell_note or "cannot execute this step in the selected isolation mode",
            hint="try --no-docker, or reproduce on the matching OS",
        )
    rendered, _secrets, unresolved = render_expressions(
        failed_step.run or "",
        combo=combo,
        github=github,
        env={**analysis.merged_env, **plan["exec_env"]},
    )
    if unresolved:
        ctx.ui.warn(
            "some workflow expressions could not be resolved locally and were left "
            "as-is (they may fail differently than in CI)"
        )
    target_cwd = ctx.repo_root
    if failed_step.working_directory:
        target_cwd = ctx.repo_root / failed_step.working_directory
    plan["cwd"] = target_cwd
    plan["target_entries"] = [
        {
            "comment": f"failing step: {failed_step.display_name}",
            "kind": "script",
            "text": rendered,
            "shell": plan["shell"],
            "cwd": target_cwd,
            "timeout": ctx.cfg.execution.command_timeout_s,
        }
    ]
    return plan


def pick_shell(
    failed_shell: str | None, job_shell: str | None, runner_os: str, docker: bool
) -> tuple[str | None, str | None]:
    """Choose the local interpreter for a step; (shell, note) — None shell is fatal."""
    explicit = (failed_shell or job_shell or "").strip().lower()
    if docker:
        if explicit in ("", "bash", "sh", "default"):
            return "bash", None
        return None, (
            f"step shell {explicit!r} cannot run inside the Docker image; "
            "run locally instead (--no-docker)"
        )
    if explicit in ("bash", "sh"):
        return ("sh" if explicit == "sh" else "bash"), None
    if explicit in ("pwsh", "powershell"):
        return "pwsh", None
    if explicit in ("cmd", "batch"):
        return "cmd", None
    if explicit in ("python",):
        return None, f"step shell {explicit!r} is not supported for local reproduction"
    default_shell = "pwsh" if runner_os == "windows" else "bash"
    return default_shell, None


def _runner_os(analysis: JobAnalysis) -> str:
    return analysis.runner_os if analysis.runner_os in ("linux", "windows", "macos") else "linux"


def _strip_sudo(command: str) -> str:
    lowered = command.lstrip()
    if lowered.startswith("sudo ") or lowered.startswith("sudo\t"):
        return command.replace("sudo", "", 1).lstrip()
    return command


def _containerize(command: str) -> str:
    """Rewrite a command so it runs in the python/ubuntu container."""
    stripped = command.strip()
    if stripped.startswith("pip ") or stripped.startswith("pip3 "):
        return "python -m " + stripped
    return stripped


def _venv_path_overlay(ctx: AppContext, venv_py: Path) -> dict[str, str]:
    bin_dir = venv_py.parent
    path = os.environ.get("PATH", "")
    return {"PATH": f"{bin_dir}{os.pathsep}{path}"}


def _build_exec_env(
    ctx: AppContext, run: RunInfo, analysis: JobAnalysis, step: WorkflowStep
) -> dict[str, str]:
    """Literal environment for the failing step (never secret values)."""
    env: dict[str, str] = {
        "CI": "true",
        "GITHUB_REPOSITORY": f"{run.owner}/{run.repo}",
        "GITHUB_SHA": run.head_sha,
        "GITHUB_REF_NAME": run.head_branch or "",
        "GITHUB_RUN_ID": str(run.id),
        "GITHUB_WORKFLOW": run.workflow_name,
        "GITHUB_EVENT_NAME": run.event,
    }
    github = github_context_vars(run)
    for name, value in {**analysis.merged_env, **step.env}.items():
        rendered, _secrets, unresolved = render_expressions(value, combo={}, github=github, env=env)
        if unresolved or "secrets." in value:
            continue  # cannot reproduce secret/context values; leave unset
        env[name] = rendered
    return env


def _audit_entries(ctx: AppContext, entries: list[dict], *, in_docker: bool, what: str) -> None:
    findings = 0
    for entry in entries:
        for finding in audit_command_text(entry.get("text") or ""):
            if in_docker and finding.kind == "elevation":
                continue
            findings += 1
            ctx.ui.warn(
                f"{what}: {finding.reason} — line {finding.line} of "
                f"{entry.get('comment', 'command')}: {finding.text[:90]}"
            )
    if findings and not ctx.ui.confirm(
        "The commands above could damage this machine or hit the network. Continue anyway?",
        default=False,
    ):
        raise ReproNotPossibleError(
            "aborted: audited commands were not confirmed",
            hint="re-run with --yes to proceed, or use --docker for isolation",
        )


# --------------------------------------------------------------------------
# Execution + verdict
# --------------------------------------------------------------------------


def _run_entries(
    ctx: AppContext, entries: list[dict], plan: dict
) -> tuple[list[dict], ExecResult | None]:
    results: list[dict] = []
    last: ExecResult | None = None
    docker_exec: DockerExecutor | None = None
    if plan["use_docker"]:
        docker_exec = DockerExecutor(plan["image"])
    for entry in entries:
        timeout = entry.get("timeout", ctx.cfg.execution.command_timeout_s)
        ctx.ui.status(f"running: {entry.get('comment', '')[:80]}")
        env_extra = dict(plan["exec_env"])
        cwd = Path(entry["cwd"]) if entry.get("cwd") else ctx.repo_root
        result, comment = _run_entry(
            ctx, entry, exec_env=env_extra, docker_exec=docker_exec, cwd=cwd, timeout=timeout
        )
        last = result
        ok = result.ok
        if result.timed_out:
            ctx.ui.warn(f"{comment}: timed out after {timeout:g}s")
        elif result.launch_error:
            ctx.ui.error(f"{comment}: {result.launch_error}")
        elif ok:
            ctx.ui.info(f"  {comment} — ok ({humanize_duration(result.duration_s)})")
        else:
            ctx.ui.warn(f"  {comment} — exit code {result.returncode}")
        results.append(
            {
                "comment": comment,
                "ok": ok,
                "exit_code": result.returncode,
                "timed_out": result.timed_out,
                "launch_error": result.launch_error,
                "duration_s": round(result.duration_s, 2),
            }
        )
    return results, last


def _run_entry(
    ctx: AppContext,
    entry: dict,
    *,
    exec_env: dict[str, str],
    docker_exec: DockerExecutor | None,
    cwd: Path,
    timeout: float,
) -> tuple[ExecResult, str]:
    comment = entry.get("comment", "")
    if docker_exec is not None:
        if entry.get("kind") == "argv":
            return docker_exec.run_argv(
                ctx.repo_root, list(entry["argv"]), env_extra=exec_env, timeout=timeout
            ), comment
        rel = cwd.relative_to(ctx.repo_root) if cwd != ctx.repo_root else None
        return docker_exec.run_script(
            ctx.repo_root,
            entry.get("text", ""),
            cwd_rel=str(rel) if rel else None,
            env_extra=exec_env,
            timeout=timeout,
        ), comment
    if entry.get("kind") == "argv":
        return ctx.runner.run_argv(
            list(entry["argv"]), cwd=cwd, env_extra=exec_env, timeout=timeout
        ), comment
    result, _script = ctx.runner.run_script(
        entry.get("text", ""),
        cwd=cwd,
        env_extra=exec_env,
        timeout=timeout,
        shell=entry.get("shell") or "bash",
        keep_script=ctx.cfg.output.debug,
    )
    return result, comment


def _compute_parity(
    ctx: AppContext,
    analysis: JobAnalysis,
    local_env: LocalEnvironment,
    plan: dict,
    *,
    deps_installed: bool,
    used_python: str | None,
) -> tuple[list[Difference], int]:
    diffs, parity = compare_environments(
        local_env,
        analysis,
        docker=plan["use_docker"],
        python_override=used_python,
        deps_ok=deps_installed if analysis.install_commands else None,
        isolated=bool(plan["venv"]) or plan["use_docker"],
    )
    return diffs, parity


def _local_evidence(result: ExecResult) -> FailureEvidence:
    combined = result.stdout + "\n" + result.stderr
    return analyze_log(combined, source="local reproduction")


def _compute_verdict(
    ci_evidence: FailureEvidence,
    local_evidence: FailureEvidence,
    local_failed: bool,
    parity: int,
    setup_ok: bool,
) -> tuple[Verdict, list[str], int]:
    """Returns (verdict, reason_lines, confidence)."""
    ci_tests = set(ci_evidence.failing_tests)
    local_tests = set(local_evidence.failing_tests)
    reasons: list[str] = []
    confidence = min(99, max(40, parity + 10))

    if not setup_ok:
        return (
            Verdict.PARTIALLY_REPRODUCED,
            ["setup steps failed locally — reproduction could not be completed"],
            20,
        )

    if local_failed:
        matched_tests = ci_tests & local_tests
        if ci_tests:
            if matched_tests:
                reasons.append(
                    "the same failing test(s) reproduced locally: "
                    + ", ".join(sorted(matched_tests))
                )
                confidence = min(99, parity + 5)
                return Verdict.REPRODUCED, reasons, confidence
            reasons.append("a test failed locally but it is not one that failed in CI")
        else:
            ci_category = clsf.classify(ci_evidence).category
            local_category = clsf.classify(local_evidence).category
            if ci_category != clsf.UNKNOWN and ci_category == local_category:
                reasons.append(f"CI and local failures share the same category ({ci_category})")
                return Verdict.REPRODUCED, reasons, min(90, confidence)
            reasons.append(
                "a failure occurred locally but its identity could not be "
                "confirmed against the CI evidence"
            )
        return Verdict.PARTIALLY_REPRODUCED, reasons, confidence

    # local run passed
    if parity >= PARITY_THRESHOLD:
        reasons.append(
            f"the failing command passed locally and environment parity is adequate ({parity}%)"
        )
        return Verdict.NOT_REPRODUCED, reasons, confidence
    reasons.append(
        f"the failing command passed locally but parity is only {parity}% — "
        "Replico cannot claim the CI failure is environment-independent"
    )
    return Verdict.PARTIALLY_REPRODUCED, reasons, min(50, confidence)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _requirements_fingerprint(repo_root: Path) -> str:
    parts: list[str] = []
    for pattern in ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg"):
        for path in sorted(repo_root.glob(pattern)):
            try:
                stat = path.stat()
                parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                continue
    return _sha256("\n".join(parts))
