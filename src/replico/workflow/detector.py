"""Environment and dependency detection for a workflow job.

Given a parsed job (and an optional matrix combination), figure out what CI
actually did: which runner image, which setup actions ran, which package
managers were used, and which install commands should be replayed locally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from replico.models import RunInfo, WorkflowJob

# Expression tokens: ${{ matrix.x }}, ${{ github.y }}, ${{ env.z }}, ${{ secrets.w }}
_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_SECRET_REF_RE = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_-]+)")

# ecosystem markers inside `run:` blocks
_PY_MARKERS = (
    r"\b(python|python3|py)\b",
    r"\bpip\b",
    r"\bpytest\b|pytest ",
    r"\bpoetry\b|\buv\b|\bpipenv\b",
    r"-m (pip|pytest|unittest|venv)\b",
)
_NODE_MARKERS = (r"\bnpm\b", r"\bnpx\b", r"\bnode\b", r"\byarn\b", r"\bpnpm\b", r"\btsc\b")
_GO_MARKERS = (r"\bgo (build|test|vet|mod|run)\b",)
_RUST_MARKERS = (r"\bcargo\b",)
_SETUP_TOOL = {
    "actions/setup-python": "python",
    "actions/setup-node": "node",
    "actions/setup-java": "java",
    "actions/setup-go": "go",
    "actions/setup-dotnet": "dotnet",
}


@dataclass
class InstallCommand:
    command: str
    kind: str  # pip | poetry | uv | npm | yarn | pnpm | cargo | generic
    cwd: str | None = None  # repo-relative working directory
    source_step: int = 0


@dataclass
class JobAnalysis:
    runner_image: str = "unknown"
    runner_os: str = "unknown"  # linux | windows | macos | self-hosted | unknown
    setup_items: list[str] = field(default_factory=list)
    ecosystems: list[str] = field(default_factory=list)
    install_commands: list[InstallCommand] = field(default_factory=list)
    merged_env: dict[str, str] = field(default_factory=dict)
    secret_names: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    referenced_python_version: str | None = None
    referenced_node_version: str | None = None


def render_expressions(
    text: str,
    *,
    combo: dict[str, str] | None = None,
    github: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    secret_marker: str = "__REPLICO_SECRET_UNAVAILABLE__",
) -> tuple[str, set[str], list[str]]:
    """Best-effort ``${{ }}`` rendering.

    Returns ``(rendered, secret_names, unresolved_tokens)``. Expressions that
    name a GitHub secret are replaced with a clearly-fake marker so local
    behavior differs deterministically instead of silently working or
    producing shell errors on the literal template text.
    """
    combo = combo or {}
    github = github or {}
    env = env or {}
    secrets: set[str] = set()
    unresolved: list[str] = []

    def repl(match: re.Match) -> str:
        expr = match.group(1).strip()
        if expr.startswith("matrix."):
            key = expr[len("matrix.") :].strip().strip("'\"")
            if key in combo:
                return combo[key]
        elif expr.startswith("github."):
            key = expr[len("github.") :].strip()
            if key in github:
                return github[key]
        elif expr.startswith("env."):
            key = expr[len("env.") :].strip().strip("'\"")
            if key in env:
                return env[key]
        elif expr.startswith("secrets."):
            key = expr[len("secrets.") :].strip()
            secrets.add(key)
            return secret_marker
        unresolved.append(expr)
        return match.group(0)

    rendered = _EXPR_RE.sub(repl, text)
    return rendered, secrets, list(dict.fromkeys(unresolved))


def _classify_runner(runs_on: list[str]) -> tuple[str, str]:
    """Return (image_label, os_family) for a job's runs-on."""
    for entry in runs_on:
        entry = entry.strip()
        lowered = entry.lower()
        if lowered.startswith("ubuntu-"):
            return entry, "linux"
        if lowered.startswith("windows-"):
            return entry, "windows"
        if lowered.startswith("macos-"):
            return entry, "macos"
        if lowered == "ubuntu-latest":
            return entry, "linux"
        if lowered == "windows-latest":
            return entry, "windows"
        if lowered == "macos-latest":
            return entry, "macos"
    if not runs_on:
        return "unknown", "unknown"
    first = runs_on[0].strip()
    if first.lower() in ("self-hosted",) or "${{" in first:
        return first, "self-hosted"
    return first, "unknown"


def _ecosystem_of_command(command: str) -> str | None:
    command.lower()
    if any(re.search(marker, command, re.IGNORECASE) for marker in _PY_MARKERS):
        # exclude node-ish false positives like `python` inside npm scripts
        return "python"
    if any(re.search(marker, command, re.IGNORECASE) for marker in _NODE_MARKERS):
        return "node"
    if any(re.search(marker, command) for marker in _GO_MARKERS):
        return "go"
    if any(re.search(marker, command) for marker in _RUST_MARKERS):
        return "rust"
    return None


_INSTALL_LINE_RE = re.compile(
    r"^\s*(sudo\s+)?(?P<body>.+)$",
)


def _classify_install(command: str) -> str | None:
    lowered = command.strip().lower()
    body = lowered
    if re.match(r"^(python|python3|py)(\s|-)", body):
        body = re.sub(r"^(python|python3|py)(\.exe)?\s+-m\s+", "", body)
    if body.startswith("pip ") or body.startswith("pip3 ") or body.startswith("pip install"):
        return "pip"
    if body.startswith("poetry "):
        return "poetry"
    if body.startswith("uv ") and (" pip" in body or " sync" in body or " add" in body):
        return "uv"
    if body.startswith("npm ") and (" install" in body or " ci" in body):
        return "npm"
    if body.startswith("yarn "):
        return "yarn"
    if body.startswith("pnpm ") and (" install" in body or " add" in body):
        return "pnpm"
    if body.startswith("cargo ") and (" install" in body or " build" in body):
        return "cargo"
    return None


def analyze_job(
    job: WorkflowJob,
    *,
    combo: dict[str, str] | None = None,
    github: dict[str, str] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> JobAnalysis:
    combo = combo or {}
    github = github or {}

    def render(text: str) -> str:
        rendered, _secrets, _unresolved = render_expressions(
            text, combo=combo, github=github, env=job.env
        )
        return rendered

    image, os_family = _classify_runner([render(entry) for entry in job.runs_on])
    analysis = JobAnalysis(runner_image=image, runner_os=os_family)

    merged_env = dict(job.env)
    if env_overrides:
        merged_env.update(env_overrides)

    for step in job.steps:
        if step.uses:
            action = step.uses.split("@")[0].strip()
            if action == "actions/checkout":
                analysis.setup_items.append("checkout")
            elif action in _SETUP_TOOL:
                tool = _SETUP_TOOL[action]
                version = step.with_args.get("python-version") or step.with_args.get("node-version")
                rendered_version = render(version) if version else ""
                if tool == "python":
                    analysis.setup_items.append(f"Python {rendered_version or '(latest)'}")
                    analysis.referenced_python_version = rendered_version or None
                    analysis.ecosystems.append("python")
                else:
                    analysis.setup_items.append(
                        f"{_SETUP_TOOL[action]} {rendered_version or '(latest)'}"
                    )
                    if tool == "node":
                        analysis.referenced_node_version = rendered_version or None
                    analysis.ecosystems.append(tool)
            else:
                analysis.setup_items.append(f"action {action}")
        if step.run:
            rendered = render(step.run)
            # stash secrets referenced anywhere in the step for reporting
            for match in _SECRET_REF_RE.finditer(step.run):
                analysis.secret_names.add(match.group(1))
            ecosystem = _ecosystem_of_command(rendered)
            if ecosystem and ecosystem not in analysis.ecosystems:
                analysis.ecosystems.append(ecosystem)
            for line in rendered.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                kind = _classify_install(stripped)
                if kind:
                    analysis.install_commands.append(
                        InstallCommand(
                            command=stripped,
                            kind=kind,
                            cwd=step.working_directory,
                            source_step=step.number,
                        )
                    )
        # env from each step may matter for the failing step only; but keep a
        # union of names for the fingerprint comparison.
        for key, value in step.env.items():
            rendered_value, _s, _u = render_expressions(
                value, combo=combo, github=github, env=merged_env
            )
            merged_env[key] = rendered_value
            for match in _SECRET_REF_RE.finditer(value):
                analysis.secret_names.add(match.group(1))

    # de-duplicate ecosystems while preserving order
    deduped: list[str] = []
    for ecosystem in analysis.ecosystems:
        if ecosystem not in deduped:
            deduped.append(ecosystem)
    analysis.ecosystems = deduped
    analysis.merged_env = merged_env
    return analysis


def github_context_vars(run: RunInfo) -> dict[str, str]:
    return {
        "event_name": run.event,
        "sha": run.head_sha,
        "ref": f"refs/heads/{run.head_branch}" if run.head_branch else "",
        "repository": f"{run.owner}/{run.repo}",
        "repository_owner": run.owner,
        "run_id": str(run.id),
        "workflow": run.workflow_name,
        "run_number": "",
        "actor": "",
    }
