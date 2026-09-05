"""Guards against malicious workflow content and accidental damage.

Replico executes commands found in *other people's* workflow YAML files. Two
classes of risk exist:

1. Hostile workflow authors trying to make Replico do something nasty.
2. Legit workflows that contain commands which are dangerous *locally*
   (``sudo``, wiping files, wiping the network) even though they are safe
   inside an ephemeral GitHub runner.

These guards implement the "never run elevated or destructive commands
without explicit confirmation" contract. They are pure functions over text,
so they can be unit-tested without executing anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RiskFinding:
    kind: str  # elevation | destructive | network_exec | suspicious
    line: int
    text: str
    reason: str


# Patterns that need explicit user confirmation on a *local* machine.
_ELEVATION = re.compile(r"(^|[\s;|&])sudo[\s]|(^|[\s;|&])doas[\s]|(^|[\s;|&])su[\s-]")
_ADMIN_WINDOWS = re.compile(r"(^|[\s;|&])(runas|gsudo)[\s]|Start-Process[^\n]*Verb RunAs")
_DESTRUCTIVE = [
    re.compile(r"\brm\s+(-[a-z]*[rR][a-z]*\s+)*[-/~]|rm\s+-rf\s+/"),
    re.compile(r"\brmdir\s+/[a-z]", re.IGNORECASE),
    re.compile(r"\bmkfs\b|\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r">\s*/dev/(sda|sdb|hda|nvme|disk)"),
    re.compile(r"\bshutdown\b|\breboot\b|\bpoweroff\b|Restart-Computer|Stop-Computer"),
    re.compile(r"\bchmod\s+(-[a-z]*R[a-z]*\s+)?[0-7]{3}\s+/|chown\s+(-[a-z]*R[a-z]*\s+)?[^ ]*\s+/"),
    re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f)"),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+\.git/"),
]
_NETWORK_EXEC = [
    re.compile(
        r"(curl|wget|powershell[^\n]*Invoke-WebRequest|Invoke-Expression)[^\n]*\|\s*(ba)?sh\b"
    ),
    re.compile(r"iwr[^\n]*\|iex|irm[^\n]*\|iex"),
    re.compile(r"(curl|wget)[^\n]*\|\s*(sudo\s+)?(ba)?sh\b"),
]
_SUSPICIOUS = [
    re.compile(r"\beval\s+\$?\("),
    re.compile(r"\b(base64|xxd|openssl)\b[^\n]*-d\b"),
    re.compile(r"\bcurl[^\n]*\s-o\s+/etc/|\bwget[^\n]*\s-O\s+/etc/"),
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"),  # fork bomb
]

_KIND_REASON = {
    "elevation": "runs with elevated privileges",
    "destructive": "destructive filesystem / git operation",
    "network_exec": "downloads and executes remote content",
    "suspicious": "obfuscated or unusual command",
}


def audit_command_text(script: str) -> list[RiskFinding]:
    """Scan a shell script (workflow `run:` content) for risk patterns."""
    findings: list[RiskFinding] = []
    for lineno, raw_line in enumerate(script.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        checks: list[tuple[re.Pattern[str] | list[re.Pattern[str]], str]] = [
            (_ELEVATION, "elevation"),
            (_ADMIN_WINDOWS, "elevation"),
            (_DESTRUCTIVE, "destructive"),
            (_NETWORK_EXEC, "network_exec"),
            (_SUSPICIOUS, "suspicious"),
        ]
        for pattern, kind in checks:
            for regex in pattern if isinstance(pattern, list) else [pattern]:
                if regex.search(line):
                    findings.append(
                        RiskFinding(
                            kind=kind,
                            line=lineno,
                            text=line[:200],
                            reason=_KIND_REASON[kind],
                        )
                    )
                    break
    return findings


def audit_environment(env: dict[str, str]) -> list[str]:
    """Return names of environment entries that fail validation.

    Prevents environment injection through hostile variable names.
    """
    from replico.util import valid_env_var_name

    return [name for name in env if not valid_env_var_name(name)]


def validate_env_name(name: str) -> bool:
    from replico.util import valid_env_var_name

    return valid_env_var_name(name)


def safe_join(root: Path, *parts: str) -> Path:
    """Join path parts ensuring the result stays inside ``root``.

    Defends against path traversal via malicious repository / job names.
    """
    candidate = root.joinpath(*parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        raise ValueError(f"unsafe path: {parts!r} escapes {root}") from None
    return candidate
