"""Local environment fingerprinting and CI-vs-local comparison."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from replico.util import architecture, decode_bytes, platform_label, platform_os_family
from replico.workflow.detector import JobAnalysis

_MAX_ENV_VARS = 60


def _run_version(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = decode_bytes(result.stdout)
    return output.splitlines()[0].strip()[:80] if output else None


@dataclass
class LocalEnvironment:
    os_label: str
    os_family: str
    arch: str
    python_version: str | None
    python_path: str | None
    git_version: str | None
    node_version: str | None
    docker_available: bool
    cwd: str
    relevant_env_names: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict:
        """Names only — environment values never enter the fingerprint."""
        return {
            "os": self.os_label,
            "os_family": self.os_family,
            "architecture": self.arch,
            "python_version": self.python_version,
            "python_path": self.python_path,
            "git_version": self.git_version,
            "node_version": self.node_version,
            "docker_available": self.docker_available,
            "cwd": self.cwd,
            "environment_variables": sorted(self.relevant_env_names),
        }


def _relevant_env_names() -> list[str]:
    """Environment variable *names* that look relevant (never values)."""
    names: list[str] = []
    for name in os.environ:
        upper = name.upper()
        if upper.startswith("REPLICO_"):
            continue
        if upper in ("PATH", "HOME", "USERPROFILE", "PYTHONPATH", "VIRTUAL_ENV", "CI"):
            names.append(upper)
            continue
        if upper.startswith(("GITHUB_", "CI_", "PYTHON", "NODE", "NPM", "PIP")):
            names.append(upper)
            continue
        if any(
            part in upper
            for part in ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "DATABASE", "DB_")
        ):
            names.append(name)
    return sorted(set(names))[:_MAX_ENV_VARS]


def capture_local_environment() -> LocalEnvironment:
    python_version = ".".join(str(v) for v in sys.version_info[:3])
    git = _run_version(["git", "--version"])
    node = _run_version(["node", "--version"])
    docker = shutil.which("docker") is not None
    if docker:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        docker = result.returncode == 0
    return LocalEnvironment(
        os_label=platform_label(),
        os_family=platform_os_family(),
        arch=architecture(),
        python_version=python_version,
        python_path=sys.executable,
        git_version=git,
        node_version=node,
        docker_available=docker,
        cwd=str(os.getcwd()),
        relevant_env_names=_relevant_env_names(),
    )


@dataclass
class Difference:
    ok: bool | None  # True=match, False=material difference, None=warning
    label: str
    detail: str = ""
    weight: float = 0.0


def compare_environments(
    local: LocalEnvironment,
    analysis: JobAnalysis,
    *,
    docker: bool = False,
    python_override: str | None = None,
    deps_ok: bool | None = None,
    isolated: bool = False,
) -> tuple[list[Difference], int]:
    """Compare local against the CI job; return (differences, parity %).

    ``python_override`` reports the interpreter actually used (venv/docker),
    ``deps_ok`` records whether install commands succeeded and ``isolated``
    whether execution happened in a venv/container.

    Parity is a transparent heuristic over a handful of weighted checks — it
    is reported as an estimate, never as a guarantee. Environment variable
    *values* never cross this boundary.
    """
    diffs: list[Difference] = []
    score = 0.0
    total = 0.0

    def add(ok: bool | None, label: str, detail: str, weight: float) -> None:
        nonlocal score, total
        total += weight
        if ok is True:
            score += weight
        diffs.append(Difference(ok=ok, label=label, detail=detail, weight=weight))

    if docker:
        add(
            True,
            f"runner OS {analysis.runner_image or analysis.runner_os}",
            "isolated in Docker (matching OS family)",
            30.0,
        )
    elif analysis.runner_os == local.os_family:
        add(True, f"OS {local.os_label}", f"CI: {analysis.runner_image}", 30.0)
    elif analysis.runner_os in ("linux", "windows", "macos"):
        add(
            False,
            f"OS {local.os_label}",
            f"CI runs on {analysis.runner_image} — material OS difference",
            30.0,
        )
    else:
        add(None, "OS", f"CI runner {analysis.runner_image or 'unknown'} cannot be compared", 0.0)

    local_py = python_override or local.python_version or ""
    requested = analysis.referenced_python_version
    if requested and "python" in analysis.ecosystems:
        if "." in requested:
            same_minor = local_py.startswith(requested)
            same_patch = same_minor
        else:
            same_minor = local_py.split(".")[0] == requested.split(".")[0]
            same_patch = same_minor
        if same_patch:
            add(True, f"Python {requested}", f"using {local_py}", 25.0)
        elif same_minor:
            add(True, f"Python {requested}", f"using {local_py} (patch differs)", 25.0)
        else:
            add(False, f"Python {requested}", f"using {local_py}", 25.0)
    elif "python" in analysis.ecosystems:
        if local_py:
            add(True, "Python", f"using {local_py}", 15.0)
        else:
            add(False, "Python", "not found locally", 15.0)

    if not docker and isolated:
        add(True, "isolation", "virtual environment", 5.0)

    if local.git_version:
        add(True, "git", f"{local.git_version}", 10.0)
    else:
        add(False, "git", "missing locally", 10.0)

    if analysis.install_commands:
        if deps_ok is True:
            add(True, "dependencies", "installed successfully", 15.0)
        elif deps_ok is False:
            add(False, "dependencies", "install steps failed locally", 15.0)
        else:
            add(None, "dependencies", "pending install", 15.0)
    elif "python" in analysis.ecosystems:
        add(None, "dependencies", "no install commands in workflow", 15.0)

    ci_env = _ci_referenced_env(analysis)
    if ci_env:
        add(
            False,
            "environment variables",
            f"CI references (unavailable locally): {', '.join(sorted(ci_env))}",
            10.0,
        )
    else:
        add(True, "environment variables", "no CI-only variables referenced", 10.0)

    parity = round(score / total * 100) if total else 0
    return diffs, parity


def _ci_referenced_env(analysis: JobAnalysis) -> set[str]:
    """Variables CI would set from secrets/contexts (names only)."""
    names: set[str] = set()
    for name, value in analysis.merged_env.items():
        if "${{" in value and "secrets." in value:
            names.add(name)
    return names
