"""Docker-based isolation for reproduction.

Docker gives the closest match for Linux CI runners: the container runs
``ubuntu`` / ``python`` images whose OS and tool versions we choose, and the
repository is mounted read-write at ``/workspace``. All commands inside the
container run as the container's root user — never as root on the host.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from replico.errors import SetupError
from replico.execution.runner import ExecResult
from replico.util import decode_bytes, valid_env_var_name


@dataclass
class DockerInfo:
    available: bool
    version: str | None = None


_docker_cache: DockerInfo | None = None


def docker_info(*, refresh: bool = False) -> DockerInfo:
    global _docker_cache
    if _docker_cache is not None and not refresh:
        return _docker_cache
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _docker_cache = DockerInfo(available=False)
        return _docker_cache
    version = decode_bytes(result.stdout).strip() if result.returncode == 0 else None
    _docker_cache = DockerInfo(available=result.returncode == 0, version=version)
    return _docker_cache


def _python_image_tag(request: str | None) -> str:
    if not request:
        return "3"
    import re

    match = re.match(r"^(\d+)(?:\.(\d+))?", request.strip())
    if not match:
        return "3"
    major = match.group(1)
    minor = match.group(2)
    return f"{major}.{minor}" if minor else major


def image_for(ecosystem: str, python_request: str | None = None) -> str:
    if ecosystem == "python":
        return f"python:{_python_image_tag(python_request)}-slim"
    return "ubuntu:24.04"


class DockerExecutor:
    """Drives the docker CLI to execute reproduction steps in a container."""

    def __init__(self, image: str) -> None:
        self.image = image

    def _argv(
        self,
        repo_root: Path,
        *,
        cwd_rel: str | None,
        env_extra: dict[str, str] | None,
        command: list[str],
    ) -> list[str]:
        argv = ["docker", "run", "--rm"]
        if cwd_rel:
            argv += ["--workdir", f"/workspace/{cwd_rel}"]
        else:
            argv += ["--workdir", "/workspace"]
        argv += ["-v", f"{repo_root.resolve()}:/workspace"]
        for name, value in (env_extra or {}).items():
            if valid_env_var_name(name) and "\x00" not in value:
                argv += ["-e", f"{name}={value}"]
        argv += [self.image, *command]
        return argv

    def run_argv(
        self,
        repo_root: Path,
        argv_in_container: list[str],
        *,
        cwd_rel: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 1800,
    ) -> ExecResult:
        from replico.execution.runner import Runner

        runner = Runner()
        return runner.run_argv(
            self._argv(
                repo_root,
                cwd_rel=cwd_rel,
                env_extra=env_extra,
                command=argv_in_container,
            ),
            timeout=timeout,
        )

    def run_script(
        self,
        repo_root: Path,
        script: str,
        *,
        cwd_rel: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 1800,
        interpreter: str = "bash",
    ) -> ExecResult:
        """Run a shell script inside the container.

        The script is written under the mounted repo (``.replico/tmp``) so
        the container can read it without extra mounts.
        """
        from replico.execution.runner import Runner

        scratch = repo_root / ".replico" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        import tempfile

        fd, name = tempfile.mkstemp(prefix="replico-docker-", suffix=".sh", dir=scratch)
        import os

        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        rel = Path(name).relative_to(repo_root)
        runner = Runner()
        result = runner.run_argv(
            self._argv(
                repo_root,
                cwd_rel=cwd_rel,
                env_extra=env_extra,
                command=[interpreter, "-eo", "pipefail", f"/workspace/{rel.as_posix()}"],
            ),
            timeout=timeout,
        )
        with contextlib.suppress(OSError):
            Path(name).unlink(missing_ok=True)
        return result


def require_docker() -> DockerInfo:
    info = docker_info()
    if not info.available:
        raise SetupError(
            "--docker was requested but Docker is not available on this machine "
            "(is the Docker daemon running?)",
            hint="start Docker Desktop / the docker daemon, then retry",
        )
    return info
