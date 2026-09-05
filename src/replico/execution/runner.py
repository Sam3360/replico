"""Process execution without a shell where possible.

GitHub `run:` steps are real shell scripts (``bash -eo pipefail`` on
Linux/macOS, pwsh on Windows), so reproduction legitimately needs a shell.
Safety comes from:

* argv-based execution for Replico's *own* commands (git, python, docker);
* scripts executed through an explicit interpreter, never ``shell=True``
  with interpolated user strings;
* environment overlays validated (no invalid names, no NUL);
* risk auditing + confirmation happening one layer up (security/guard).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from replico.errors import SetupError
from replico.util import decode_bytes, valid_env_var_name


@dataclass
class ExecResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    launch_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.launch_error


@dataclass
class ShellInfo:
    bash: str | None = None
    sh: str | None = None
    pwsh: str | None = None
    powershell: str | None = None
    cmd: str | None = None

    @classmethod
    def detect(cls) -> ShellInfo:
        info = cls()
        for attr, names in (
            ("bash", ("bash",)),
            ("sh", ("sh",)),
            ("pwsh", ("pwsh",)),
            ("powershell", ("powershell", "powershell.exe")),
        ):
            for name in names:
                path = shutil.which(name)
                if path:
                    setattr(info, attr, path)
                    break
        info.cmd = (
            os.environ.get("COMSPEC")
            or shutil.which("cmd")
            or ("cmd.exe" if os.name == "nt" else None)
        )
        return info


class Runner:
    """Runs argv commands and audited shell scripts."""

    def __init__(self, scratch_dir: Path | None = None) -> None:
        self.shells = ShellInfo.detect()
        self._scratch = scratch_dir
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)

    # -- environment hygiene -------------------------------------------------

    @staticmethod
    def _clean_env(env_extra: dict[str, str] | None) -> dict[str, str]:
        clean = dict(os.environ)
        if not env_extra:
            return clean
        for name, value in env_extra.items():
            if not valid_env_var_name(name) or "\x00" in value:
                continue
            clean[name] = value
        return clean

    # -- argv execution (no shell) -------------------------------------------

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path | str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 1800,
    ) -> ExecResult:
        if any("\x00" in part for part in argv):
            return ExecResult(returncode=-1, launch_error="command contains NUL bytes")
        start = time.perf_counter()
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=self._clean_env(env_extra),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return ExecResult(
                returncode=-1,
                launch_error=f"executable not found: {exc.filename or argv[0]}",
                duration_s=time.perf_counter() - start,
            )
        except PermissionError as exc:
            return ExecResult(
                returncode=-1,
                launch_error=f"permission denied launching {argv[0]}: {exc}",
                duration_s=time.perf_counter() - start,
            )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(OSError):
                process.kill()
            stdout, stderr = process.communicate(timeout=30)
        return ExecResult(
            returncode=process.returncode,
            stdout=decode_bytes(stdout or b""),
            stderr=decode_bytes(stderr or b""),
            timed_out=timed_out,
            duration_s=time.perf_counter() - start,
            launch_error=(f"timed out after {timeout:g}s" if timed_out else None),
        )

    # -- script execution (audited shell) ------------------------------------

    def _write_script(self, content: str, suffix: str) -> Path:
        if self._scratch is None:
            raise SetupError("no scratch directory configured for script execution")
        self._scratch.mkdir(parents=True, exist_ok=True)
        import tempfile

        fd, name = tempfile.mkstemp(prefix="replico-run-", suffix=suffix, dir=self._scratch)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return Path(name)

    def run_script(
        self,
        script: str,
        *,
        cwd: Path | str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 1800,
        shell: str | None = None,
        keep_script: bool = False,
    ) -> tuple[ExecResult, Path | None]:
        """Execute a multi-line script through an explicit interpreter."""
        shell = (shell or "bash").lower()
        interpreter: list[str] | None
        interpreter, script_file = self._build_interpreter(script, shell)
        if interpreter is None:
            return (
                ExecResult(
                    returncode=-1,
                    launch_error=f"cannot run {shell!r} steps locally: "
                    "no suitable interpreter found (install the shell, or use --docker)",
                ),
                None,
            )
        result = self.run_argv(interpreter, cwd=cwd, env_extra=env_extra, timeout=timeout)
        if not keep_script and script_file is not None:
            with contextlib.suppress(OSError):
                script_file.unlink(missing_ok=True)
        return result, (script_file if keep_script else None)

    def _build_interpreter(self, script: str, shell: str) -> tuple[list[str] | None, Path | None]:
        if shell in ("bash", "sh"):
            path = self.shells.bash if shell == "bash" else (self.shells.sh or self.shells.bash)
            if not path:
                return None, None
            script_file = self._write_script(script, ".sh")
            return [path, "--noprofile", "--norc", "-eo", "pipefail", str(script_file)], script_file
        if shell in ("pwsh", "powershell"):
            path = self.shells.pwsh or self.shells.powershell
            if not path:
                return None, None
            script_file = self._write_script(script, ".ps1")
            return [
                path,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_file),
            ], script_file
        if shell in ("cmd", "batch"):
            if not self.shells.cmd:
                return None, None
            script_file = self._write_script(script, ".cmd")
            return [self.shells.cmd, "/d", "/s", "/c", str(script_file)], script_file
        return None, None
