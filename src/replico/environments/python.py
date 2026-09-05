"""Python ecosystem support: interpreter discovery, venv management and the
recipe for replaying Python CI jobs locally."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from replico.environments.base import EcosystemAdapter, EcosystemDetection
from replico.errors import SetupError
from replico.util import decode_bytes
from replico.workflow.detector import JobAnalysis

_PYTHON_REQUEST_RE = re.compile(
    r"^\s*(?P<op>>=|~=|==)?\s*(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"^Python\s+(\d+)\.(\d+)\.(\d+)")

INSTALL_KINDS = ("pip", "poetry", "uv")


def parse_python_request(text: str) -> str | None:
    """Normalize a setup-python `python-version` value like '3.13' / '3.13.x'."""
    if not text or text.strip().lower() in ("auto", "latest"):
        return None
    match = _PYTHON_REQUEST_RE.match(text)
    if not match:
        return None
    major = match.group("major")
    minor = match.group("minor")
    if minor and minor.lower() in ("x", "*"):
        minor = None
    return f"{major}.{minor}" if minor else major


def _matches_request(version: tuple[int, int, int], requested: str | None) -> bool:
    """Whether an installed version satisfies a CI python-version request.

    * ``'3'``/``'3.x'`` matches any Python 3.x (GitHub's setup-python semantics).
    * ``'3.12'`` matches exactly (major, minor).
    * ``'>=3.12'`` etc. is treated as a loose floor (best effort).
    """
    if requested is None:
        return False
    text = requested.strip().lower()
    op_match = re.match(r"^(>=|~=|==)?\s*(\d+)(?:\.(\d+))?", text)
    if not op_match:
        return False
    op = op_match.group(1) or "=="
    major = int(op_match.group(2))
    minor = op_match.group(3)
    if minor is None or minor.lower() in ("x", "*"):
        # Major-only request: any same-major satisfies it.
        return version[0] == major
    minor_i = int(minor)
    if op == ">=":
        return (version[0], version[1]) >= (major, minor_i)
    return (version[0], version[1]) == (major, minor_i)


def probe_python_version(executable: str) -> tuple[int, int, int] | None:
    """Return the version of a python executable, or None on any failure."""
    try:
        result = subprocess.run(
            [executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(decode_bytes(result.stdout))
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    text = decode_bytes(result.stdout).strip()
    parts = text.split(".")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None


@dataclass
class PythonRuntime:
    executable: str
    version: tuple[int, int, int] | None
    how: str = ""
    matches_request: bool = False


def find_python(request: str | None) -> PythonRuntime:
    """Locate a python interpreter matching (or closest to) the request."""
    requested = parse_python_request(request) if request else None
    target: tuple[int, int] | None = None
    if requested and "." in requested:
        major_s, minor_s = requested.split(".", 1)
        target = (int(major_s), int(minor_s))

    candidates: list[str] = []
    if target:
        major, minor = target
        for name in (f"python{major}.{minor}", f"python{major}{minor}", f"python{major}"):
            candidates.append(name)
    candidates += ["python3", "python"]

    seen: set[str] = set()
    runtime: PythonRuntime | None = None
    for name in candidates:
        path = shutil.which(name)
        if not path or path in seen:
            continue
        seen.add(path)
        version = probe_python_version(path)
        if version is None:
            continue
        match = _matches_request(version, requested)
        if runtime is None or match:
            runtime = PythonRuntime(
                executable=path, version=version, how=f"found {name} on PATH", matches_request=match
            )
        if match:
            break

    # Windows `py` launcher can install/select specific versions.
    if sys.platform.startswith("win") and (runtime is None or not runtime.matches_request):
        py_launcher = shutil.which("py")
        if py_launcher:
            versions_to_try = [f"-{target[0]}.{target[1]}"] if target else []
            versions_to_try.append("-3")
            for flag in versions_to_try:
                version = probe_python_version(f"{py_launcher} {flag}") or _probe_py_launcher(
                    py_launcher, flag
                )
                if version:
                    candidate = PythonRuntime(
                        executable=f"{py_launcher} {flag}",
                        version=version,
                        how=f"py launcher {flag}",
                        matches_request=_matches_request(version, requested),
                    )
                    if candidate.matches_request:
                        return candidate
                    if runtime is None:
                        runtime = candidate
    if runtime is None:
        raise SetupError(
            "no Python interpreter found on this machine (looked for " + ", ".join(candidates) + ")"
        )
    return runtime


def _probe_py_launcher(launcher: str, flag: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [launcher, flag, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = decode_bytes(result.stdout).strip()
    parts = text.split(".")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None


def venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_venv(venv_dir: Path, runtime: PythonRuntime) -> Path:
    """Create (or reuse) the isolated venv used for reproduction."""
    marker = venv_dir / ".replico-venv.json"
    version_str = ".".join(str(part) for part in runtime.version) if runtime.version else "unknown"
    reuse = False
    if marker.is_file() and venv_python(venv_dir).exists():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            reuse = (
                info.get("interpreter") == runtime.executable and info.get("version") == version_str
            )
        except (OSError, ValueError):
            reuse = False
    if reuse:
        return venv_python(venv_dir)
    # Only ever delete a venv we created ourselves (marker present).
    if venv_dir.exists() and marker.is_file():
        shutil.rmtree(venv_dir, ignore_errors=True)
    venv_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [runtime.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        detail = decode_bytes(result.stderr).strip().splitlines()
        raise SetupError(
            "could not create the reproduction virtual environment: "
            + (detail[-1] if detail else "python -m venv failed")
        )
    marker.write_text(
        json.dumps({"interpreter": runtime.executable, "version": version_str}),
        encoding="utf-8",
    )
    return venv_python(venv_dir)


class PythonAdapter(EcosystemAdapter):
    name = "python"

    def detect(self, analysis: JobAnalysis) -> EcosystemDetection:
        is_python = "python" in analysis.ecosystems or any(
            cmd.kind in INSTALL_KINDS for cmd in analysis.install_commands
        )
        if not is_python:
            return EcosystemDetection(
                ecosystem="python",
                supported=False,
                reason="not the ecosystem for this job",
            )
        return EcosystemDetection(
            ecosystem="python",
            supported=True,
            python_request=analysis.referenced_python_version,
            installs=[cmd for cmd in analysis.install_commands if cmd.kind in INSTALL_KINDS],
            notes=[],
        )


class GenericAdapter(EcosystemAdapter):
    """Plain `run:`-only jobs with no package managers to install."""

    name = "generic"

    def detect(self, analysis: JobAnalysis) -> EcosystemDetection:
        has_ecosystem = bool(analysis.ecosystems)
        has_installs = bool(analysis.install_commands)
        if has_ecosystem or has_installs:
            return EcosystemDetection(
                ecosystem="generic", supported=False, reason="job uses a managed ecosystem"
            )
        return EcosystemDetection(
            ecosystem="generic",
            supported=True,
            reason="plain shell job without managed dependencies",
        )


class _UnsupportedAdapter(EcosystemAdapter):
    """Template for ecosystems planned after v0.1 (Node, Go, Rust, ...)."""

    name = ""
    marker = ""
    _install_kinds: tuple[str, ...] = ()

    def detect(self, analysis: JobAnalysis) -> EcosystemDetection:
        is_mine = self.name in analysis.ecosystems or any(
            cmd.kind in self._install_kinds for cmd in analysis.install_commands
        )
        if not is_mine:
            return EcosystemDetection(
                ecosystem=self.name,
                supported=False,
                reason="not the ecosystem for this job",
            )
        return EcosystemDetection(
            ecosystem=self.name,
            supported=False,
            reason=(
                f"the {self.name} ecosystem is not supported in replico v0.1 "
                f"(detected via {self.marker}); it is planned for v0.3"
            ),
        )


class NodeAdapter(_UnsupportedAdapter):
    name = "node"
    marker = "actions/setup-node / npm / yarn / pnpm"
    _install_kinds = ("npm", "yarn", "pnpm")


class GoAdapter(_UnsupportedAdapter):
    name = "go"
    marker = "actions/setup-go / go build/test"
    _install_kinds = ("go",)


class RustAdapter(_UnsupportedAdapter):
    name = "rust"
    marker = "cargo"
    _install_kinds = ("cargo",)


ADAPTERS: list[EcosystemAdapter] = [
    PythonAdapter(),
    NodeAdapter(),
    GoAdapter(),
    RustAdapter(),
    GenericAdapter(),
]
