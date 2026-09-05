"""The auxiliary commands: status, diff, env, clean, config, capture."""

from __future__ import annotations

import os

from replico.analysis.whyfail_adapter import describe_reason, render_diagnosis
from replico.config import Config, config_to_mapping
from replico.environments.fingerprint import capture_local_environment
from replico.errors import InvalidInputError
from replico.flows import (
    Outcome,
    _build_payload,  # noqa: F401 (unused hint)
    attempt_local_diagnosis,
    build_plan_from_payload,
)
from replico.github.refs import RunRef, parse_run_id, parse_run_url
from replico.models import Verdict
from replico.pipeline import AppContext
from replico.security.redaction import is_sensitive_env_name
from replico.util import short_sha


def cmd_status(ctx: AppContext) -> Outcome:
    ui = ctx.ui
    ui.rule("REPLICO STATUS")
    if not ctx.store.exists():
        raise InvalidInputError(
            f"no saved reproduction at {ctx.store.dir} — run `replico <run-url>` first"
        )
    payload = ctx.store.read()
    run = payload.get("run") or {}
    verdict = (payload.get("verdict") or {}).get("verdict")
    parity = payload.get("parity")

    ui.kv("Saved reproduction", f"available ({ctx.store.dir})")
    ui.kv("Repository", f"{run.get('owner')}/{run.get('repo')}")
    ui.kv("Workflow", run.get("workflow_name") or "?")
    ui.kv("Run", f"#{run.get('run_id')}")
    ui.kv("Verdict", str(verdict or "none"))
    if parity is not None:
        ui.kv("Environment parity (saved)", f"{parity}%")
    saved_sha = str(run.get("head_sha") or "")
    ui.kv("Saved commit", short_sha(saved_sha) if saved_sha else "unknown")

    repo = ctx.repo
    changed_files: list[str] = []
    if repo is not None and repo.head_sha:
        ui.kv("Current commit", short_sha(repo.head_sha))
        if saved_sha and repo.head_sha != saved_sha:
            changed_files = repo.changed_since(saved_sha)
            ui.warn(f"{len(changed_files)} file(s) changed since the reproduction")
        elif repo.dirty_files:
            changed_files = [f for f in repo.dirty_files if f not in changed_files]
            ui.warn(f"working tree modified: {len(repo.dirty_files)} file(s) (not committed)")
        else:
            ui.ok("working tree matches the reproduced commit")
    else:
        ui.kv("Current commit", "not a git checkout")

    reruns = payload.get("reruns") or []
    if reruns:
        ui.out("")
        ui.out("Rerun history:")
        for index, entry in enumerate(reruns, start=1):
            ui.out(
                f"  {index}. {entry.get('verdict', '?')} at "
                f"{short_sha(str(entry.get('head_sha') or '')) or '?'}"
                f" ({len(entry.get('failing_tests') or [])} failing tests)"
            )

    _render_status_whyfail(ui, payload)

    whyfail_info = _whyfail_status_info(payload)
    doc = {
        "tool": "replico",
        "command": "status",
        "exit_code": 0,
        "saved_reproduction": str(ctx.store.dir),
        "run": {
            "run_id": run.get("run_id"),
            "repository": f"{run.get('owner')}/{run.get('repo')}",
            "workflow": run.get("workflow_name"),
            "head_sha": run.get("head_sha"),
        },
        "verdict": verdict,
        "parity": parity,
        "current_commit": repo.head_sha if repo else None,
        "changed_files_since_reproduction": len(changed_files),
        "working_tree_dirty": bool(repo and repo.dirty_files),
        "whyfail": whyfail_info,
    }
    return Outcome(exit_code=0, verdict=Verdict.NOT_REPRODUCED, doc=doc)


def cmd_diff(ctx: AppContext) -> Outcome:
    ui = ctx.ui
    ui.rule("REPLICO DIFF")
    if not ctx.store.exists():
        raise InvalidInputError(
            f"no saved reproduction at {ctx.store.dir} — run `replico <run-url>` first"
        )
    payload = ctx.store.read()
    run = payload.get("run") or {}
    saved_sha = str(run.get("head_sha") or "")
    repo = ctx.repo
    if repo is None or not repo.head_sha:
        raise InvalidInputError("replico diff requires a git checkout")

    ui.kv("Saved commit", short_sha(saved_sha) if saved_sha else "unknown")
    ui.kv("Current commit", short_sha(repo.head_sha))

    stat = ""
    changed_files: list[str] = []
    if saved_sha and repo.has_commit(saved_sha):
        stat = repo.diff_stat_since(saved_sha)
        changed_files = repo.changed_since(saved_sha)
        if stat:
            ui.out(stat)
            ui.out("")
    if repo.dirty_files:
        ui.out(f"Working tree (uncommitted): {len(repo.dirty_files)} file(s) modified")
        for name in repo.dirty_files[:20]:
            ui.out(f"  ~ {name}")
        ui.out("")

    if changed_files or repo.dirty_files:
        ui.warn(
            f"{len(changed_files) + len(repo.dirty_files)} file(s) changed since "
            "the reproduced CI failure"
        )
    else:
        ui.ok("no changes since the reproduced commit")

    diffs = payload.get("differences") or []
    if diffs:
        ui.out("")
        ui.out("Recorded environment differences (at reproduction time):")
        for diff in diffs:
            mark = {True: "✓", False: "✗", None: "⚠"}.get(diff.get("ok"), "?")
            ui.out(f"  {mark} {diff.get('label')}: {diff.get('detail')}")
        if payload.get("parity") is not None:
            ui.kv("Environment parity", f"{payload.get('parity')}%")

    _render_diff_whyfail(ui, payload)

    doc = {
        "tool": "replico",
        "command": "diff",
        "exit_code": 0,
        "saved_commit": saved_sha,
        "current_commit": repo.head_sha,
        "changed_files": changed_files,
        "uncommitted": repo.dirty_files,
        "whyfail": _whyfail_diff_info(payload),
    }
    return Outcome(exit_code=0, verdict=Verdict.NOT_REPRODUCED, doc=doc)


def cmd_env(ctx: AppContext) -> Outcome:
    ui = ctx.ui
    local_env = capture_local_environment()
    ui.rule("REPLICO ENVIRONMENT")
    ui.kv("OS", local_env.os_label)
    ui.kv("Architecture", local_env.arch)
    ui.kv("Python", local_env.python_version or "not found")
    ui.kv("Git", local_env.git_version or "not found")
    if local_env.node_version:
        ui.kv("Node", local_env.node_version)
    ui.kv("Docker", "available" if local_env.docker_available else "not available")
    ui.kv("Working directory", local_env.cwd)
    ui.out("")

    sensitive = sorted(name for name in os.environ if is_sensitive_env_name(name))
    if sensitive:
        ui.out("Secret-like environment variables (value never displayed):")
        for name in sensitive:
            ui.out(f"  {name} = present")
        ui.out("")

    relevant = [name for name in local_env.relevant_env_names if not is_sensitive_env_name(name)]
    if relevant:
        ui.out(f"Other relevant variables ({len(relevant)}):")
        ui.out("  " + ", ".join(relevant[:40]))
        ui.out("")

    ui.out("Secrets in this output are names only. Replico never prints values.")
    doc = local_env.to_mapping()
    doc.update({"tool": "replico", "command": "env", "exit_code": 0})
    return Outcome(exit_code=0, verdict=Verdict.NOT_REPRODUCED, doc=doc)


def cmd_clean(ctx: AppContext) -> Outcome:
    ui = ctx.ui
    ui.rule("REPLICO CLEAN")
    if not ctx.store.exists():
        ui.out("nothing to clean — no .replico/ reproduction found")
        return Outcome(exit_code=0, verdict=None, doc={"command": "clean", "removed": False})
    if not ui.confirm(
        f"This removes {ctx.store.dir}/ (reproduction data and its virtual environment). Continue?",
        default=False,
    ):
        raise InvalidInputError("clean aborted")
    ctx.store.clean()
    ui.ok(f"removed {ctx.store.dir}/")
    return Outcome(exit_code=0, verdict=None, doc={"command": "clean", "removed": True})


def cmd_config(cfg: Config) -> Outcome:
    mapping = config_to_mapping(cfg)
    return Outcome(exit_code=0, verdict=None, doc=mapping)


def cmd_capture(ctx: AppContext, job: str | None, step: str | None) -> Outcome:
    """Capture CI context into .replico/ (intended for `if: failure()` steps)."""
    ui = ctx.ui
    ui.rule("REPLICO CAPTURE")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not repo_full or not run_id:
        raise InvalidInputError(
            "replico capture reads GitHub-provided environment variables "
            "(GITHUB_REPOSITORY, GITHUB_RUN_ID) and is meant for CI steps like:\n"
            "  - name: Save reproduction metadata\n"
            "    if: failure()\n"
            "    run: replico capture"
        )
    owner, _, repo_name = repo_full.partition("/")
    if not owner or not repo_name:
        raise InvalidInputError(f"invalid GITHUB_REPOSITORY value: {repo_full!r}")
    local_env = capture_local_environment()
    payload: dict[str, object] = {
        "tool_version": None,
        "run": {
            "run_id": int(run_id),
            "owner": owner,
            "repo": repo_name,
            "workflow_id": int(os.environ.get("GITHUB_WORKFLOW_ID", 0) or 0),
            "workflow_name": os.environ.get("GITHUB_WORKFLOW_NAME")
            or os.environ.get("GITHUB_WORKFLOW", ""),
            "head_sha": os.environ.get("GITHUB_SHA", ""),
            "head_branch": os.environ.get("GITHUB_REF_NAME", ""),
            "event": os.environ.get("GITHUB_EVENT_NAME", ""),
            "conclusion": "failure",
            "html_url": f"https://github.com/{repo_full}/actions/runs/{run_id}",
        },
        "jobs": [],
        "failure": {
            "job_name": job or os.environ.get("GITHUB_JOB", ""),
            "job_id": None,
            "step_name": step or "",
            "step_number": None,
            "tests": [],
            "summary": "captured in CI — reproduce locally with `replico run <id>`",
            "category_hint": "",
            "evidence_lines": [],
            "evidence_line_numbers": [],
            "source": f"GitHub Actions capture (job {job or '?'})",
        },
        "classification": {"category": "UNKNOWN", "confidence": 0, "explanation": []},
        "workflow_yaml": "",
        "environment": local_env.to_mapping(),
        "differences": [],
        "parity": None,
        "execution": {},
        "verdict": {"verdict": "unsupported", "confidence": 0, "reasons": [], "exit_code": 0},
        "setup_commands": [],
        "target_commands": [],
        "reruns": [],
    }
    ctx.store.write(payload)
    ui.ok(f"captured CI metadata into {ctx.store.dir}")
    ui.out(f"Locally, reproduce this run from your checkout with:\n    replico run {run_id}")
    doc = {
        "tool": "replico",
        "command": "capture",
        "exit_code": 0,
        "run_id": int(run_id),
        "repository": repo_full,
        "saved_reproduction": str(ctx.store.dir),
    }
    return Outcome(exit_code=0, verdict=Verdict.UNSUPPORTED, doc=doc)


def cmd_diagnose(ctx: AppContext, *, whyfail_flag: bool = True) -> Outcome:
    """Diagnose the most recently reproduced local failure with WhyFail.

    Re-runs the saved failing command under WhyFail's structured CLI. Fully
    offline — no GitHub or network access. If the saved reproduction has no
    diagnosable Python failure (or WhyFail is unavailable), the command
    reports that honestly instead of pretending.
    """
    ui = ctx.ui
    ui.rule("REPLICO DIAGNOSE")
    if not ctx.store.exists():
        raise InvalidInputError(
            f"no saved reproduction at {ctx.store.dir} — run `replico <run-url>` first"
        )
    payload = ctx.store.read()
    exec_info = payload.get("execution") or {}
    target_cmds = exec_info.get("target_commands") or []
    if not target_cmds:
        raise InvalidInputError("saved reproduction has no target command to diagnose")

    target = target_cmds[0]
    plan = build_plan_from_payload(ctx, payload)
    rel = target.get("cwd_rel")
    plan["cwd"] = ctx.repo_root / rel if rel else ctx.repo_root
    plan["target_entries"] = [target]

    # ``replico diagnose`` is itself the diagnosis request: attempt whenever
    # the saved reproduction is a diagnosable Python failure (auto mode).
    # ``--whyfail`` is accepted for clarity and has no additional effect —
    # WhyFail is the only diagnostic engine.
    result, reason = attempt_local_diagnosis(
        ctx,
        ecosystems=["python"] if plan.get("venv") else [],
        plan=plan,
        local_failed=True,
        flag=None,
    )
    if result is not None:
        render_diagnosis(ui, result)
        doc = result.to_doc()
    else:
        ui.warn(f"WhyFail diagnosis skipped: {describe_reason(reason)}")
        doc = {
            "tool": "whyfail",
            "version": None,
            "available": False,
            "diagnosed": False,
            "reason": reason or "skipped",
            "source": "local_reproduction",
            "exit_code": None,
            "diagnostics": [],
        }
    ctx.store.write_whyfail(doc)
    payload["whyfail"] = doc
    ctx.store.write(payload)
    return Outcome(
        exit_code=0,
        verdict=None,
        doc={"tool": "replico", "command": "diagnose", "exit_code": 0, "whyfail": doc},
    )


# ---------------------------------------------------------------------------
# WhyFail helpers for status/diff
# ---------------------------------------------------------------------------


def _whyfail_headline(doc: dict | None) -> tuple[str | None, str | None]:
    """(exception_type, confidence) of the primary saved diagnostic."""
    if not doc or not doc.get("diagnosed"):
        return None, None
    diagnostics = doc.get("diagnostics") or []
    if not diagnostics:
        return None, None
    primary = diagnostics[0]
    cause = (primary.get("diagnosis") or {}).get("cause") or {}
    return primary.get("exception_type"), cause.get("confidence")


def _render_status_whyfail(ui, payload: dict) -> None:
    whyfail_doc = payload.get("whyfail")
    ui.out("")
    ui.out("WhyFail:")
    if not whyfail_doc or not whyfail_doc.get("available"):
        ui.out("  not applicable")
        if whyfail_doc and whyfail_doc.get("reason"):
            ui.out(f"  ({describe_reason(str(whyfail_doc.get('reason')))})", style="dim")
        return
    if not whyfail_doc.get("diagnosed"):
        reason = str(whyfail_doc.get("reason") or "no diagnosis")
        ui.out(f"  not applicable ({describe_reason(reason)})")
        return
    exception_type, confidence = _whyfail_headline(whyfail_doc)
    if exception_type:
        ui.out(f"  diagnosis available — {exception_type}")
    else:
        ui.out("  diagnosis available")
    if confidence:
        ui.out(f"  confidence: {str(confidence).upper()}")


def _whyfail_status_info(payload: dict) -> dict:
    whyfail_doc = payload.get("whyfail")
    if not whyfail_doc:
        return {"available": False, "diagnosed": False, "reason": "not_attempted"}
    exception_type, confidence = _whyfail_headline(whyfail_doc)
    return {
        "available": bool(whyfail_doc.get("available")),
        "diagnosed": bool(whyfail_doc.get("diagnosed")),
        "reason": whyfail_doc.get("reason"),
        "exception_type": exception_type,
        "confidence": confidence,
    }


def _render_diff_whyfail(ui, payload: dict) -> None:
    reruns = payload.get("reruns") or []
    whyfail_doc = payload.get("whyfail")
    if not whyfail_doc or not whyfail_doc.get("diagnosed"):
        return
    if not reruns:
        return
    latest = reruns[-1].get("diagnosis") or {}
    ui.out("")
    ui.out("Diagnosis (WhyFail):")
    original_exception, _ = _whyfail_headline(whyfail_doc)
    ui.out(f"  original: {original_exception or '?'}")
    if not latest.get("diagnosed"):
        reason = str(latest.get("reason") or "not applicable")
        ui.out(f"  last rerun: no diagnosis ({describe_reason(reason)})")
    else:
        summary = latest.get("summary") or {}
        exception_type = summary.get("exception_type") or "?"
        confidence = summary.get("confidence")
        line = f"  last rerun: {exception_type}"
        if confidence:
            line += f" ({str(confidence).upper()})"
        ui.out(line)


def _whyfail_diff_info(payload: dict) -> dict:
    reruns = payload.get("reruns") or []
    whyfail_doc = payload.get("whyfail")
    info: dict = {"available": bool(whyfail_doc and whyfail_doc.get("available"))}
    original_exception, original_confidence = _whyfail_headline(whyfail_doc)
    info["original_exception"] = original_exception
    info["original_confidence"] = original_confidence
    if reruns:
        latest = reruns[-1].get("diagnosis") or {}
        summary = latest.get("summary") or {}
        info["current_exception"] = (
            summary.get("exception_type") if latest.get("diagnosed") else None
        )
        info["current_confidence"] = summary.get("confidence") if latest.get("diagnosed") else None
        info["changed"] = info["current_exception"] != original_exception
    return info


def parse_ref_token(token: str, ctx: AppContext) -> RunRef:
    """Parse a positional argument: a run URL or a numeric run id."""
    from replico.github.refs import looks_like_run_id

    if token.startswith("http://") or token.startswith("https://"):
        return parse_run_url(token)
    if looks_like_run_id(token):
        run_id = parse_run_id(token)
        owner = repo = ""
        if ctx.repo and ctx.repo.remote:
            owner = ctx.repo.remote.owner
            repo = ctx.repo.remote.repo
        if not owner or not repo:
            owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
            repo_full = os.environ.get("GITHUB_REPOSITORY", "")
            if repo_full and "/" in repo_full:
                owner, _, repo = repo_full.partition("/")
        if not owner or not repo:
            raise InvalidInputError(
                "run id given but the local repository has no GitHub origin — "
                "run from a checkout of the repository or pass a full run URL"
            )
        return RunRef(owner=owner, repo=repo, run_id=run_id)
    raise InvalidInputError(
        f"cannot interpret {token!r} — expected a GitHub Actions run URL like\n"
        "https://github.com/<owner>/<repo>/actions/runs/<run-id>"
    )
