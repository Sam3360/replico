"""Small platform / string utilities used across Replico."""

from __future__ import annotations

import os
import platform
import re
import shlex
import sys
from pathlib import Path

# Names GitHub permits for repositories/owners. Anything else is rejected
# before it can reach a URL, a filesystem path or a subprocess argument.
# Also rejects dot-only, dot-edged and double-dot names (path traversal).
SAFE_NAME_RE = re.compile(r"^(?!\.)(?!.*\.\.)(?!.*\.$)[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeNameError(ValueError):
    """Raised when a name fails validation (defense in depth)."""


def validate_repo_name(name: str) -> str:
    """Validate an owner/repo name; raise if it could abuse paths or URLs."""
    if not SAFE_NAME_RE.match(name):
        raise UnsafeNameError(
            f"invalid GitHub name {name!r}: only letters, digits, '.', '_', '-' are allowed"
        )
    return name


def validate_sha(sha: str, *, label: str = "commit") -> str:
    if not SHA_RE.match(sha):
        raise ValueError(f"invalid {label} sha {sha!r}")
    return sha


def valid_env_var_name(name: str) -> bool:
    """Only names safe to place into a subprocess environment."""
    return bool(_VAR_NAME_RE.match(name)) and "=" not in name


def platform_os_family() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system.startswith("windows"):
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def platform_label() -> str:
    system = platform.system()
    if system == "Windows":
        release = platform.release()
        try:
            build = int(release)
        except Exception:  # noqa: BLE001
            build = 0
        if build >= 10:
            return "Windows 10/11"
        return f"Windows {release}"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0] or '?'}"
    if system == "Linux":
        return "Linux"
    return system or "unknown"


def architecture() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine or platform.processor() or "unknown"


def is_tty(stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def find_upwards(filename: str, start: Path | None = None) -> Path | None:
    """Walk from `start` (default: cwd) upwards looking for `filename`."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def command_tokenize(text: str) -> list[str]:
    """Split a shell-ish command line into argv without executing anything.

    Handles single/double quotes and backslash escapes. No expansion is
    performed — only use when the target command is known to be plain.
    """
    try:
        return shlex.split(text, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"cannot parse command {text!r}: {exc}") from exc


def short_sha(sha: str) -> str:
    return sha[:7] if sha and len(sha) > 7 else sha


def contains_shell_meta(text: str) -> bool:
    """True when a line needs real shell semantics (pipes, redirects, ...)."""
    return any(char in text for char in "|&;<>`$") or text.lstrip().startswith(
        ("#", "cd ", "export ")
    )


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
