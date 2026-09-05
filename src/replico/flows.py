"""End-to-end flows: reproduce a CI run and rerun a saved reproduction.

``flows.reproduce`` is the flagship path — the code equivalent of the
product's promise:

    failed CI run → analyze → reconstruct environment → run locally → verdict
"""

from __future__ import annotations

import os
from pathlib import Path

from replico import __version__
from replico.analysis import classifier as clsf
from replico.analysis.whyfail_adapter import (
    WhyFailResult,
    describe_reason,
    find_python_invocation,
    render_diagnosis,
    run_diagnosis,
    should_diagnose,
    whyfail_available,
)
from replico.config import Config
from replico.environments import ADAPTERS, choose_adapter
from replico.environments.fingerprint import (
    Difference,
    capture_local_environment,
)
from replico.environments.python import ensure_venv, find_python
from replico.errors import (
    InvalidInputError,
    ReplicoError,
    ReproNotPossibleError,
    UnsupportedError,
)
from replico.execution.docker import DockerExecutor
from replico.execution.runner import Runner
from replico.github.client import GitHubClient, NotFoundError
from replico.github.refs import RunRef
from replico.gitrepo import GitRepo, find_git_repo
from replico.models import JobInfo, StepRunInfo, Verdict
from replico.pipeline import (
    AppContext,
    Outcome,
    ReproOptions,
    _analyze,
    _audit_entries,
    _compute_parity,
    _compute_verdict,
    _find_workflow_step,
    _local_evidence,
    _outcome_for_verdict,
    _pick_isolation,
    _plan_execution,
    _require_local_repo,
    _resolve_run,
    _run_entries,
    _select_failed_job,
    _select_failed_step,
    find_venv_dir,
)
from replico.security.guard import audit_command_text
from replico.security.redaction import Sanitizer
from replico.storage.store import ReproductionStore
from replico.ui import UI, humanize_duration
from replico.util import short_sha
from replico.workflow.matcher import match_api_job
from replico.workflow.parser import parse_workflow

# --------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------


def build_app(
    cfg: Config,
    ui: UI,
    sanitizer: Sanitizer,
    cwd: Path | None = None,
    repo: GitRepo | None = None,
    client: GitHubClient | None = None,
) -> AppContext:
    cwd = (cwd or Path.cwd()).resolve()
    repo = repo if repo is not None else find_git_repo(cwd)
    root = repo.root if repo else cwd
    store = ReproductionStore(root, sanitizer=sanitizer)
    runner = Runner(scratch_dir=root / ".replico" / "tmp")
    return AppContext(
        cfg=cfg,
        ui=ui,
        sanitizer=sanitizer,
        cwd=cwd,
        repo=repo,
        store=store,
        client=client,
        runner=runner,
    )


# --------------------------------------------------------------------------
# Reproduce
# --------------------------------------------------------------------------


def reproduce(ctx: AppContext, ref: RunRef | None, options: ReproOptions) -> Outcome:
    ui = ctx.ui
    ui.rule("REPLICO")
    ui.out("CI failures, reproduced locally.", style="dim")

    if ref is not None:
        # Fail fast (and offline) when we are not inside the right checkout.
        _require_local_repo(ctx, ref.owner, ref.repo)
    run, client = _resolve_run(ctx, ref, options)

    if options.clean_first and ctx.store.exists():
        ui.warn("--clean: removing previous .replico/ reproduction")
        ctx.store.clean()

    # -- jobs ---------------------------------------------------------------
    if client is not None:
        jobs = client.get_jobs(RunRef(owner=run.owner, repo=run.repo, run_id=run.id))
    else:
        payload = ctx.store.read()
        jobs = [
            JobInfo(
                id=int(entry.get("id", 0)),
                name=str(entry.get("name", "")),
                conclusion=entry.get("conclusion"),
                steps=[
                    StepRunInfo(
                        number=int(s.get("number", 0)),
                        name=str(s.get("name", "")),
                        conclusion=s.get("conclusion"),
                    )
                    for s in entry.get("steps", [])
                ],
            )
            for entry in payload.get("jobs", [])
        ]
        if not jobs:
            raise InvalidInputError("offline mode has no saved job data — run once online first")

    api_job = _select_failed_job(ctx, run, jobs, options)
    api_step = _select_failed_step(api_job, options)
    ui.ok(f"failed job: {api_job.name}")
    ui.ok(f"failed step: {api_step.name}")

    # -- workflow -----------------------------------------------------------
    workflow = None
    workflow_yaml = ""
    if client is not None:
        try:
            path = RunRef(owner=run.owner, repo=run.repo, run_id=run.id)
            workflow_path = client.get_workflow_path(path, run.workflow_id)
            text = None
            for sha in (run.head_sha, run.head_branch or run.head_sha):
                if not sha:
                    continue
                try:
                    text = client.get_file_content(path, workflow_path, sha)
                    break
                except NotFoundError:
                    continue
            if text is None:
                raise UnsupportedError(
                    f"could not retrieve {workflow_path} at {short_sha(run.head_sha)}"
                )
            workflow_yaml = text
            workflow = parse_workflow(text, workflow_path)
        except (UnsupportedError, InvalidInputError):
            raise
        except ReplicoError:
            raise
        except Exception as exc:  # noqa: BLE001 - any other failure blocks planning
            raise UnsupportedError(
                f"could not read the workflow that produced run #{run.id}: {exc}"
            ) from exc
    else:
        saved_yaml = ctx.store.read_workflow_yaml()
        if saved_yaml:
            workflow = parse_workflow(saved_yaml, "workflow.yml (saved)")

    if workflow is None:
        raise UnsupportedError("no workflow definition available to plan a reproduction")

    match = match_api_job(workflow, api_job.name)
    if match is None:
        raise UnsupportedError(
            f"job {api_job.name!r} does not appear in workflow {workflow.display_name} "
            "— the workflow may have changed since the run"
        )
    workflow_job = match.job
    failed_workflow_step = _find_workflow_step(workflow_job, api_step)
    if failed_workflow_step is None or not failed_workflow_step.run:
        if failed_workflow_step is not None and failed_workflow_step.uses:
            raise UnsupportedError(
                f"the failing step {api_step.name!r} is a third-party action "
                f"({failed_workflow_step.uses}) — Replico replays `run:` steps in v0.1"
            )
        raise UnsupportedError(
            f"could not map failing step {api_step.name!r} to a runnable `run:` step"
        )

    # -- analysis ------------------------------------------------------------
    run_ref = RunRef(owner=run.owner, repo=run.repo, run_id=run.id)
    analysis, evidence = _analyze(
        ctx, client, run_ref, run, api_job, api_step, workflow_job, match.combo
    )
    classification = clsf.classify(evidence)

    # -- plan ----------------------------------------------------------------
    detection = choose_adapter(analysis, ADAPTERS)
    if not detection.supported:
        raise UnsupportedError(detection.reason)

    runtime = None
    want_local_python = detection.ecosystem == "python" and not (
        options.docker is True or ctx.cfg.execution.mode == "docker"
    )
    if want_local_python:
        request = analysis.referenced_python_version
        if not request and ctx.cfg.python.preferred_version not in ("auto", ""):
            request = ctx.cfg.python.preferred_version
        runtime = find_python(request)

    use_docker, docker_reason = _pick_isolation(ctx, analysis, options, runtime)

    ui.section("Reproduction plan")
    _render_plan(
        ui,
        run,
        api_job,
        api_step,
        workflow,
        analysis,
        detection,
        use_docker,
        docker_reason,
        evidence,
    )

    will_write = bool(analysis.install_commands) or use_docker or want_local_python
    risky = bool(audit_command_text(failed_workflow_step.run or ""))
    if (will_write or risky) and not ui.confirm(
        "Replico will create .replico/ artifacts and run the commands above locally. Continue?",
        default=False,
    ):
        raise ReproNotPossibleError(
            "reproduction not started (declined confirmation)",
            hint="re-run with --yes to accept automatically",
        )

    # -- local environment ---------------------------------------------------
    local_env = capture_local_environment()
    ui.section("Environment")
    _render_environment(ui, local_env)

    plan = _plan_execution(
        ctx,
        run,
        analysis,
        workflow_job,
        failed_workflow_step,
        api_step,
        detection,
        use_docker,
        match.combo,
    )
    if plan.get("shell_note"):
        ui.warn(plan["shell_note"])
    _audit_entries(
        ctx,
        plan["setup_entries"] + plan["target_entries"],
        in_docker=use_docker,
        what="reproduction commands",
    )

    # -- execute -------------------------------------------------------------
    ui.section("Reproducing")
    setup_results, _ = _run_entries(ctx, plan["setup_entries"], plan)
    target_results, target_result = _run_entries(ctx, plan["target_entries"], plan)
    setup_ok = all(entry["ok"] for entry in setup_results)
    if target_result is None:
        raise ReproNotPossibleError("nothing was executed — no verdict possible")

    # -- verdict -------------------------------------------------------------
    local_failed = not target_result.ok
    used_python = None
    runtime = plan.get("runtime")
    if runtime is not None and runtime.version:
        used_python = ".".join(str(v) for v in runtime.version)
    elif use_docker and analysis.referenced_python_version:
        used_python = analysis.referenced_python_version
    diffs, parity = _compute_parity(
        ctx,
        analysis,
        local_env,
        plan,
        deps_installed=setup_ok,
        used_python=used_python,
    )
    local_evidence = _local_evidence(target_result)
    verdict, reasons, confidence = _compute_verdict(
        evidence, local_evidence, local_failed, parity, setup_ok
    )

    ui.section("Failure analysis")
    _render_failure(ctx, evidence, classification)
    ui.section("Environment differences")
    _render_differences(ui, diffs, parity)
    ui.section("Local result")
    _render_local_result(ui, target_result, local_evidence)

    # -- diagnosis (WhyFail) ---------------------------------------------------
    # Only ever runs when the local reproduction actually failed AND the
    # failing step is a python/pytest invocation WhyFail can diagnose.
    whyfail_result: WhyFailResult | None = None
    whyfail_reason = ""
    if local_failed or options.diagnose is True:
        whyfail_result, whyfail_reason = attempt_local_diagnosis(
            ctx,
            ecosystems=analysis.ecosystems,
            plan=plan,
            local_failed=local_failed,
            flag=options.diagnose,
        )
        if whyfail_result is not None:
            ui.section("WhyFail diagnosis")
            render_diagnosis(ui, whyfail_result)
        elif options.diagnose is True:
            ui.warn(f"WhyFail diagnosis skipped: {describe_reason(whyfail_reason)}")
    else:
        # The local command passed — nothing to diagnose (kept explicit so the
        # saved record can say WHY diagnosis was not attempted).
        whyfail_reason = "no_local_failure"

    exit_code = _outcome_for_verdict(verdict)
    ui.rule()
    ui.out(_result_banner(verdict), style=_style_for(verdict))
    for reason in reasons:
        ui.out(f"  • {reason}")
    ui.out("")
    if verdict is Verdict.NOT_REPRODUCED:
        ui.out("Replico cannot claim reproduction — CI may be failing for an")
        ui.out("environment-specific reason; the differences above explain why.")

    whyfail_doc = _whyfail_payload_doc(whyfail_result, whyfail_reason)
    classification.whyfail = whyfail_result.summary() if whyfail_result else None
    payload = _build_payload(
        ctx,
        run,
        jobs,
        api_job,
        api_step,
        workflow_yaml,
        analysis,
        evidence,
        classification,
        plan,
        setup_results,
        target_results,
        verdict,
        reasons,
        confidence,
        exit_code,
        diffs,
        parity,
        local_env,
        used_python,
        whyfail_doc=whyfail_doc,
    )
    ctx.store.write(payload)
    _persist_whyfail_artifact(ctx, whyfail_result, whyfail_doc)
    ui.ok(f"saved reproduction to {ctx.store.dir}")

    doc = _doc_from_payload(payload, verdict, exit_code)
    headline = _headline_for_verdict(verdict, not local_failed, local_evidence)
    return Outcome(exit_code=exit_code, verdict=verdict, doc=doc, headline=headline)


# --------------------------------------------------------------------------
# Rerun
# --------------------------------------------------------------------------


def rerun(ctx: AppContext, options: ReproOptions) -> Outcome:
    ui = ctx.ui
    ui.rule("REPLICO RERUN")
    if not ctx.store.exists():
        raise InvalidInputError(
            f"no saved reproduction at {ctx.store.dir} — run `replico <run-url>` first"
        )
    if ctx.repo is None:
        raise InvalidInputError("rerun must run inside the repository checkout")
    payload = ctx.store.read()
    run_meta = payload.get("run") or {}
    exec_info = payload.get("execution") or {}
    saved_sha = str(run_meta.get("head_sha") or "")
    ui.ok(f"using saved reproduction: {ctx.store.dir}")
    ui.kv("Saved commit", short_sha(saved_sha))
    ui.kv("Current commit", short_sha(ctx.repo.head_sha) if ctx.repo.head_sha else "?")

    changed: list[str] = []
    if ctx.repo.head_sha and saved_sha and ctx.repo.head_sha != saved_sha:
        changed = ctx.repo.changed_since(saved_sha)
    if ctx.repo.dirty_files:
        ui.warn(f"working tree is modified ({len(ctx.repo.dirty_files)} files)")

    # Recreate the isolation from the saved record.
    plan = build_plan_from_payload(ctx, payload)

    installs = exec_info.get("install_commands") or []
    target_cmds = exec_info.get("target_commands") or []
    if installs:
        from replico.pipeline import _requirements_fingerprint, _sha256

        current_hash = _sha256(
            "\n".join(cmd.get("text", "") for cmd in installs)
            + _requirements_fingerprint(ctx.repo_root)
        )
        reuse = current_hash == exec_info.get("install_hash", "") and plan.get("venv") is not None
        for cmd in installs:
            if not reuse:
                ui.status(f"running {cmd.get('comment', 'install')}…")
                cwd = _cwd_for(ctx, cmd)
                _exec_simple_script(ctx, plan, cmd, cwd)
        ui.ok("environment ready")
        if reuse:
            ui.info("dependencies unchanged — reused the existing environment")
    else:
        ui.ok("environment ready")

    if not target_cmds:
        raise InvalidInputError("saved reproduction has no target command")

    ui.ok("running the previously failing step…")
    target = target_cmds[0]
    result = _exec_simple_script(ctx, plan, target, _cwd_for(ctx, target))
    local_evidence = _local_evidence(result)

    ci_tests = list(payload.get("failure", {}).get("tests") or [])
    ci_category = (payload.get("classification") or {}).get("category")
    now_failed = not result.ok
    local_tests = local_evidence.failing_tests
    matched = sorted(set(ci_tests) & set(local_tests))

    if not now_failed:
        verdict = Verdict.NOT_REPRODUCED
        headline = "The previously reproduced failure no longer occurs locally."
        reasons = ["the failing command now passes locally"]
        if ci_tests:
            reasons.append("previously failing tests: " + ", ".join(ci_tests))
    elif matched:
        verdict = Verdict.REPRODUCED
        headline = "The reproduced failure still exists locally."
        reasons = ["the same test(s) still fail locally: " + ", ".join(matched)]
    else:
        same_category = bool(ci_category) and (
            clsf.classify(local_evidence).category == ci_category
        )
        if not ci_tests and same_category:
            verdict = Verdict.REPRODUCED
            headline = "A failure matching CI still occurs locally."
            reasons = [f"failure category {ci_category} still reproduces"]
        else:
            verdict = Verdict.PARTIALLY_REPRODUCED
            headline = "A different failure now occurs locally."
            reasons = ["the previously reproduced failure appears to be gone"]
            if local_tests:
                reasons.append("still failing locally: " + ", ".join(local_tests))
            if ci_tests:
                reasons.append("CI failing tests were: " + ", ".join(ci_tests))
    exit_code = _outcome_for_verdict(verdict)

    ui.section("Local result")
    _render_local_result(ui, result, local_evidence)

    # -- diagnosis (WhyFail) ---------------------------------------------------
    whyfail_result: WhyFailResult | None = None
    whyfail_reason = ""
    if now_failed or options.diagnose is True:
        plan["target_entries"] = [target]
        plan["cwd"] = _cwd_for(ctx, target)
        whyfail_result, whyfail_reason = attempt_local_diagnosis(
            ctx,
            ecosystems=["python"] if plan.get("venv") else [],
            plan=plan,
            local_failed=now_failed,
            flag=options.diagnose,
        )
        if whyfail_result is not None:
            ui.section("WhyFail diagnosis")
            render_diagnosis(ui, whyfail_result)
        elif options.diagnose is True:
            ui.warn(f"WhyFail diagnosis skipped: {describe_reason(whyfail_reason)}")
    else:
        whyfail_reason = "no_local_failure"

    ui.rule()
    ui.out(_result_banner(verdict), style=_style_for(verdict))
    for reason in reasons:
        ui.out(f"  • {reason}")
    ui.out("")
    ui.kv("Headline", headline)
    if verdict is Verdict.NOT_REPRODUCED:
        ui.out("Wording is careful by design: this does not prove CI will pass — it")
        ui.out("only means the previously reproduced failure no longer occurs locally.")

    reruns = list(payload.get("reruns") or [])
    rerun_entry: dict = {
        "head_sha": ctx.repo.head_sha,
        "changed_since_saved": len(changed),
        "exit_code": result.returncode,
        "failing_tests": local_tests,
        "verdict": verdict.value,
    }
    if whyfail_result is not None:
        rerun_entry["diagnosis"] = {
            "diagnosed": whyfail_result.diagnosed,
            "reason": whyfail_result.reason,
            "summary": whyfail_result.summary(),
        }
    reruns.append(rerun_entry)
    payload["reruns"] = reruns
    whyfail_doc = _whyfail_payload_doc(whyfail_result, whyfail_reason)
    payload["whyfail"] = whyfail_doc
    ctx.store.write(payload)
    _persist_whyfail_artifact(ctx, whyfail_result, whyfail_doc)

    doc = _doc_from_payload(payload, verdict, exit_code)
    return Outcome(exit_code=exit_code, verdict=verdict, doc=doc, headline=headline)


def build_plan_from_payload(ctx: AppContext, payload: dict) -> dict:
    """Rebuild execution state (venv, PATH, docker image) from a saved record.

    Used by ``rerun`` and ``diagnose`` so both replay a saved reproduction
    under the same interpreter/isolation that produced it.
    """
    exec_info = payload.get("execution") or {}
    mode = exec_info.get("mode")
    plan: dict = {
        "use_docker": mode == "docker",
        "image": exec_info.get("image"),
        "runtime": None,
        "venv": None,
        "exec_env": {},
        "shell": "bash",
        "setup_entries": [],
        "target_entries": [],
        "cwd": ctx.repo_root,
    }
    if mode == "local_venv":
        request = exec_info.get("python_request") or None
        runtime = find_python(request)
        plan["runtime"] = runtime
        venv_py = ensure_venv(find_venv_dir(ctx), runtime)
        plan["venv"] = venv_py
        bin_dir = venv_py.parent
        plan["exec_env"]["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return plan


def _cwd_for(ctx: AppContext, cmd: dict) -> Path:
    rel = cmd.get("cwd_rel")
    if rel:
        return ctx.repo_root / rel
    return ctx.repo_root


def _exec_simple_script(ctx: AppContext, plan: dict, cmd: dict, cwd: Path):
    env = dict(plan.get("exec_env") or {})
    text = cmd.get("text", "")
    if plan.get("use_docker"):
        docker_exec = DockerExecutor(plan["image"])
        rel = cwd.relative_to(ctx.repo_root) if cwd != ctx.repo_root else None
        return docker_exec.run_script(
            ctx.repo_root,
            text,
            cwd_rel=str(rel) if rel else None,
            env_extra=env,
        )
    result, _script = ctx.runner.run_script(
        text, cwd=cwd, env_extra=env, shell=cmd.get("shell") or "bash"
    )
    return result


# --------------------------------------------------------------------------
# WhyFail diagnosis helpers
# --------------------------------------------------------------------------


def attempt_local_diagnosis(
    ctx: AppContext,
    *,
    ecosystems: list[str],
    plan: dict,
    local_failed: bool,
    flag: bool | None,
) -> tuple[WhyFailResult | None, str]:
    """Attempt WhyFail diagnosis of a locally reproduced Python failure.

    Returns ``(result, reason_token)``. ``result`` is not None whenever an
    attempt was actually made — regardless of whether WhyFail produced a
    diagnosis. A None result with a reason token means no attempt happened
    (not a Python failure, disabled, no local failure, ...).
    """
    script, python_exe = _diagnosis_inputs(plan)
    invocation = find_python_invocation(script, python_exe) if python_exe else None
    attempt, reason = should_diagnose(
        config_enabled=ctx.cfg.diagnostics.enabled,
        config_whyfail=ctx.cfg.diagnostics.whyfail,
        flag=flag,
        ecosystems=ecosystems,
        local_failed=local_failed,
        invocation=invocation,
    )
    if not attempt:
        return None, reason
    result = run_diagnosis(
        ctx.runner,
        python_exe=python_exe or "",
        invocation=invocation or [],
        cwd=Path(plan.get("cwd") or ctx.repo_root),
        timeout=float(ctx.cfg.execution.command_timeout_s),
        sanitizer=ctx.sanitizer,
        extra_env=dict(plan.get("exec_env") or {}),
    )
    return result, ""


def _diagnosis_inputs(plan: dict) -> tuple[str, str | None]:
    """(script_text, python_exe) for the failing step in a plan."""
    python_exe: str | None = None
    venv = plan.get("venv")
    runtime = plan.get("runtime")
    if venv:
        python_exe = str(venv)
    elif runtime is not None and getattr(runtime, "executable", None):
        python_exe = str(runtime.executable)
    entries = plan.get("target_entries") or []
    script = str(entries[0].get("text") or "") if entries else ""
    return script, python_exe


def _whyfail_payload_doc(result: WhyFailResult | None, reason: str) -> dict:
    """The ``whyfail`` block stored in the reproduction payload/JSON."""
    if result is not None:
        return result.to_doc()
    return {
        "tool": "whyfail",
        "version": None,
        "available": whyfail_available(),
        "diagnosed": False,
        "reason": reason or "not_attempted",
        "source": "local_reproduction",
        "exit_code": None,
        "diagnostics": [],
    }


def _persist_whyfail_artifact(ctx: AppContext, result: WhyFailResult | None, doc: dict) -> None:
    """Write (or clear) the ``.replico/whyfail.json`` artifact.

    The artifact is written whenever a diagnosis was attempted (even when it
    produced no structured diagnostics — the explicit state is recorded); it
    is removed when no attempt happened so a stale diagnosis never lingers.
    """
    if result is not None:
        ctx.store.write_whyfail(doc)
    else:
        ctx.store.remove_whyfail()


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _render_plan(
    ui, run, api_job, api_step, workflow, analysis, detection, use_docker, docker_reason, evidence
) -> None:
    ui.kv("Repository", f"{run.owner}/{run.repo}")
    ui.kv("Commit", short_sha(run.head_sha))
    ui.kv("Workflow", workflow.display_name)
    ui.kv("Job", api_job.name)
    ui.kv("Failed step", api_step.name)
    ui.kv("Runner", analysis.runner_image or "unknown")
    ui.kv("Ecosystem", detection.ecosystem)
    ui.kv("Execution", f"Docker ({docker_reason})" if use_docker else docker_reason)
    ui.out("")
    ui.out("Detected setup:")
    for item in analysis.setup_items or ["(no setup actions found)"]:
        ui.out(f"  ✓ {item}")
    if analysis.install_commands:
        ui.out("")
        ui.out("Detected installs:")
        for cmd in analysis.install_commands:
            ui.out(f"  • {cmd.command[:100]}")
    if evidence.summary:
        ui.out("")
        ui.out("CI failure signal:")
        ui.out(f"  {evidence.summary[:200]}")


def _render_environment(ui: UI, local_env) -> None:
    ui.kv("OS", local_env.os_label)
    ui.kv("Architecture", local_env.arch)
    ui.kv("Python", local_env.python_version or "not found")
    if local_env.node_version:
        ui.kv("Node", local_env.node_version)
    if local_env.git_version:
        ui.kv("Git", local_env.git_version)
    ui.kv("Docker", "available" if local_env.docker_available else "not available")
    ui.kv("Working directory", local_env.cwd)
    if local_env.relevant_env_names:
        ui.info(
            f"{len(local_env.relevant_env_names)} relevant environment variables "
            "(names only — values are never shown)"
        )


def _render_differences(ui: UI, diffs: list[Difference], parity: int) -> None:
    for diff in diffs:
        if diff.ok is True:
            ui.out(f"  ✓ {diff.label}: {diff.detail}", style="green")
        elif diff.ok is False:
            ui.out(f"  ✗ {diff.label}: {diff.detail}", style="red")
        else:
            ui.out(f"  ⚠ {diff.label}: {diff.detail}", style="yellow")
    ui.out("")
    ui.kv("Environment parity", f"{parity}% (estimate — not a guarantee)")


def _render_failure(ctx, evidence, classification) -> None:
    ui = ctx.ui
    ui.kv("Category", f"{classification.category} (confidence {classification.confidence}%)")
    for line in classification.explanation:
        ui.out(f"  {line}")
    if evidence.failing_tests:
        ui.out("")
        ui.out("Failing tests (CI):")
        for test in evidence.failing_tests[:20]:
            ui.out(f"  ✗ {test}", style="red")
    if evidence.lines:
        ui.out("")
        ui.out(
            f"Relevant evidence ({len(evidence.lines)} lines from {evidence.source or 'CI logs'}):"
        )
        for number, line in zip(evidence.line_numbers, evidence.lines, strict=False):
            ui.out(f"  {number}: {line[:220]}", style="dim")
    if ctx.sanitizer.known_secret_count:
        ui.info(
            f"{ctx.sanitizer.known_secret_count} known secret values were redacted "
            "from display and saved artifacts"
        )


def _render_local_result(ui: UI, result, local_evidence) -> None:
    if result.launch_error:
        ui.error(f"could not run the failing step locally: {result.launch_error}")
        return
    if result.timed_out:
        ui.warn(f"local run timed out after {result.duration_s:.0f}s")
    elif result.ok:
        ui.out("  ✓ the failing command PASSED locally", style="green")
    else:
        ui.out(
            f"  ✗ the failing command FAILED locally (exit {result.returncode}, "
            f"{humanize_duration(result.duration_s)})",
            style="red",
        )
    if local_evidence.failing_tests:
        ui.out("")
        ui.out("Failing tests (local):")
        for test in local_evidence.failing_tests[:20]:
            ui.out(f"  ✗ {test}", style="red")
    tail = (result.stdout or "").strip().splitlines()[-12:]
    if tail:
        ui.out("")
        ui.out("Local output (tail):")
        for line in tail:
            ui.out(f"  {line[:200]}", style="dim")


def _result_banner(verdict: Verdict) -> str:
    return {
        Verdict.REPRODUCED: "REPLICO RESULT — CI FAILURE REPRODUCED",
        Verdict.NOT_REPRODUCED: "REPLICO RESULT — CI FAILURE NOT REPRODUCED",
        Verdict.PARTIALLY_REPRODUCED: "REPLICO RESULT — PARTIALLY REPRODUCED",
        Verdict.UNSUPPORTED: "REPLICO RESULT — UNSUPPORTED",
    }[verdict]


def _style_for(verdict: Verdict) -> str:
    return {
        Verdict.REPRODUCED: "bold red",
        Verdict.NOT_REPRODUCED: "bold green",
        Verdict.PARTIALLY_REPRODUCED: "bold yellow",
        Verdict.UNSUPPORTED: "bold magenta",
    }[verdict]


def _headline_for_verdict(verdict: Verdict, local_passed: bool, local_evidence) -> str:
    if verdict is Verdict.REPRODUCED:
        if local_evidence.failing_tests:
            return "CI failure reproduced locally: " + local_evidence.failing_tests[0]
        return "CI failure reproduced locally."
    if verdict is Verdict.NOT_REPRODUCED:
        return "CI failure NOT reproduced locally (local run passed)."
    if verdict is Verdict.PARTIALLY_REPRODUCED:
        if not local_passed:
            return "A failure occurred locally but could not be confirmed as CI's."
        return "Could not reproduce: environment differences prevent a claim."
    return "Reproduction unsupported."


# --------------------------------------------------------------------------
# Persistence / JSON
# --------------------------------------------------------------------------


def _build_payload(
    ctx: AppContext,
    run,
    jobs: list[JobInfo],
    api_job,
    api_step,
    workflow_yaml: str,
    analysis,
    evidence,
    classification,
    plan: dict,
    setup_results: list[dict],
    target_results: list[dict],
    verdict: Verdict,
    reasons: list[str],
    confidence: int,
    exit_code: int,
    diffs: list[Difference],
    parity: int,
    local_env,
    used_python: str | None,
    whyfail_doc: dict | None = None,
) -> dict:
    setup_commands = [
        {
            "comment": entry.get("comment", ""),
            "text": entry.get("text", ""),
            "shell": entry.get("shell", "bash"),
            "cwd_rel": _rel_cwd(ctx, entry.get("cwd")),
        }
        for entry in plan["setup_entries"]
    ]
    target_commands = [
        {
            "comment": entry.get("comment", ""),
            "text": entry.get("text", ""),
            "shell": entry.get("shell", "bash"),
            "cwd_rel": _rel_cwd(ctx, entry.get("cwd")),
        }
        for entry in plan["target_entries"]
    ]
    mode = "docker" if plan["use_docker"] else ("local_venv" if plan.get("venv") else "local")
    plan.get("runtime")
    return {
        "tool_version": __version__,
        "run": {
            "run_id": run.id,
            "owner": run.owner,
            "repo": run.repo,
            "workflow_id": run.workflow_id,
            "workflow_name": run.workflow_name,
            "head_sha": run.head_sha,
            "head_branch": run.head_branch,
            "event": run.event,
            "conclusion": run.conclusion,
            "html_url": run.html_url,
        },
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "conclusion": job.conclusion,
                "steps": [
                    {"number": s.number, "name": s.name, "conclusion": s.conclusion}
                    for s in job.steps
                ],
            }
            for job in jobs
        ],
        "failure": {
            "job_name": api_job.name,
            "job_id": api_job.id,
            "step_name": api_step.name,
            "step_number": api_step.number,
            "tests": evidence.failing_tests,
            "summary": evidence.summary,
            "category_hint": evidence.category_hint,
            "evidence_lines": evidence.lines,
            "evidence_line_numbers": evidence.line_numbers,
            "source": evidence.source,
        },
        "classification": classification.to_mapping(),
        "workflow_yaml": workflow_yaml,
        "environment": {
            **local_env.to_mapping(),
            "python_used": used_python,
            "python_request": analysis.referenced_python_version,
            "mode": mode,
            "image": plan.get("image"),
            "venv": str(plan["venv"]) if plan.get("venv") else None,
        },
        "differences": [
            {"ok": diff.ok, "label": diff.label, "detail": diff.detail, "weight": diff.weight}
            for diff in diffs
        ],
        "parity": parity,
        "execution": {
            "mode": mode,
            "image": plan.get("image"),
            "python_used": used_python,
            "python_request": analysis.referenced_python_version,
            "install_hash": plan.get("install_hash", ""),
            "install_commands": setup_commands,
            "target_commands": target_commands,
            "setup_results": setup_results,
            "target_results": target_results,
            "target_exit_code": (target_results[-1].get("exit_code") if target_results else None),
            "target_ok": (target_results[-1].get("ok") if target_results else None),
            "deps_ok": all(entry.get("ok") for entry in setup_results),
            "shell": plan.get("shell"),
        },
        "verdict": {
            "verdict": verdict.value,
            "confidence": confidence,
            "reasons": reasons,
            "exit_code": exit_code,
        },
        "setup_commands": setup_commands,
        "target_commands": target_commands,
        "reruns": [],
        "whyfail": whyfail_doc
        or {
            "tool": "whyfail",
            "version": None,
            "available": False,
            "diagnosed": False,
            "reason": "not_attempted",
            "source": "local_reproduction",
            "exit_code": None,
            "diagnostics": [],
        },
    }


def _rel_cwd(ctx: AppContext, cwd) -> str | None:
    if not cwd:
        return None
    try:
        return os.path.relpath(str(cwd), ctx.repo_root)
    except ValueError:
        return None


def _doc_from_payload(payload: dict, verdict: Verdict, exit_code: int) -> dict:
    """Machine-readable result document (used by --json)."""
    run = payload["run"]
    failure = payload["failure"]
    env = payload["environment"]
    execution = payload["execution"]
    target = (execution.get("target_results") or [{}])[-1]
    return {
        "tool": "replico",
        "status": verdict.value,
        "exit_code": exit_code,
        "repository": f"{run['owner']}/{run['repo']}",
        "run_id": run["run_id"],
        "commit": run["head_sha"],
        "workflow": run.get("workflow_name") or "",
        "job": failure["job_name"],
        "step": failure["step_name"],
        "failure": {
            "tests": failure["tests"],
            "summary": failure["summary"],
            "evidence": {
                "source": failure.get("source", ""),
                "lines": failure.get("evidence_lines", []),
            },
        },
        "classification": payload["classification"],
        "environment": {
            "os": env.get("os"),
            "architecture": env.get("architecture"),
            "python_used": env.get("python_used"),
            "python_request": env.get("python_request"),
            "mode": env.get("mode"),
        },
        "environment_parity": payload.get("parity"),
        "differences": payload.get("differences", []),
        "verdict": payload["verdict"],
        "local_result": {
            "ok": target.get("ok"),
            "exit_code": target.get("exit_code"),
            "timed_out": target.get("timed_out"),
        },
        "whyfail": payload.get("whyfail")
        or {
            "tool": "whyfail",
            "version": None,
            "available": False,
            "diagnosed": False,
            "reason": "not_attempted",
            "source": "local_reproduction",
            "exit_code": None,
            "diagnostics": [],
        },
        "saved_reproduction": ".replico/",
    }
