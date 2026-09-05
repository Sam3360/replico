"""Redaction: SecretShield integration + literal known-value redaction.

SecretShield (https://github.com/Sam3360/secretshield) provides pattern and
high-entropy secret detection/redaction, plus ``enable()`` which protects
``sys.stdout``/``sys.stderr`` and the ``logging`` module. Replico uses the
public SecretShield API:

* ``secretshield.redact(text, entropy_threshold, redact_with)``
* ``secretshield.detect(text, entropy_threshold)`` -> list[Match]
* ``secretshield.configure(...)`` / ``get_config`` / ``enable`` / ``disable``

SecretShield is content-based: it cannot know that a *low-entropy literal*
such as ``purplemonkeydishwasher42`` is your database password. Replico
therefore wraps it in :class:`Sanitizer`, which first removes known secret
*values* (collected from the environment / config) and then hands the result
to SecretShield for pattern + entropy redaction. The rest of Replico only
ever talks to :class:`Sanitizer`; SecretShield's internals stay behind this
adapter.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

import secretshield  # type: ignore[import-untyped]

REDACTED = "[REDACTED]"

# Names whose values must never be displayed or persisted.
_SECRET_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "auth",
    "credential",
    "client_secret",
    "session",
    "bearer",
)
SECRET_ENV_NAME_RE = re.compile(r"(^|_)(" + "|".join(_SECRET_PARTS) + r")($|_)", re.IGNORECASE)
_URL_CRED_RE = re.compile(r"://[^/@\s]+@", re.IGNORECASE)


def is_sensitive_env_name(name: str) -> bool:
    """True when a variable name strongly suggests a secret value."""
    upper = name.upper()
    if upper.startswith("REPLICO_"):
        return False
    # Not secrets despite matching keyword shapes:
    if upper in ("PWD", "OLDPWD"):
        return False
    return bool(SECRET_ENV_NAME_RE.search(name))


def collect_environment_secrets(
    environ: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
    minimum_length: int = 6,
) -> list[str]:
    """Known secret values from the environment.

    Only values whose *names* look sensitive are collected — ordinary
    environment values are not treated as secrets. Values shorter than
    ``minimum_length`` are skipped because replacing them everywhere would
    corrupt ordinary log text.
    """
    environ = os.environ if environ is None else environ
    values: list[str] = []
    for name, value in environ.items():
        if is_sensitive_env_name(name) and len(value) >= minimum_length and value.strip():
            values.append(value)
    for value in extra:
        if value and len(value) >= minimum_length and value.strip():
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def describe_env_var(name: str, value: str | None) -> str:
    """A display-safe description of one environment variable.

    Secret-looking values collapse to ``present``/``absent``; the raw value
    is never included.
    """
    if value is None:
        return f"{name} = absent"
    if is_sensitive_env_name(name):
        return f"{name} = present"
    return f"{name} = present"


def _literal_redact(text: str, secrets: Iterable[str], mask: str) -> tuple[str, bool]:
    redacted = False
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, mask)
            redacted = True
    return text, redacted


_ASSIGNMENT_VALUE_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(?P<quote>[\"']?)(?P<value>[^\s\"';{}]+)(?P=quote)"
)
# Assignment keys whose values should always be masked when they look like
# credentials. This is the documented compatibility layer: SecretShield is
# pattern/entropy based and will not mask e.g. ``key=<aws-secret>`` without
# the familiar ``AWS_SECRET_ACCESS_KEY=`` context.
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"auth|credential|client[_-]?secret|session|bearer|pwd|key)$",
    re.IGNORECASE,
)


def _mask_secret_assignments(text: str, mask: str) -> tuple[str, bool]:
    changed = False

    def _replace(match: re.Match) -> str:
        nonlocal changed
        name = match.group("name")
        value = match.group("value")
        quote = match.group("quote") or ""
        if not _ASSIGNMENT_SECRET_RE.search(name):
            return match.group(0)
        if len(value) < 12 or value.startswith(("$", "http://", "https://", '"/')):
            return match.group(0)
        if value.endswith((".yml", ".yaml", ".json", ".toml", ".lock", ".py", ".sh")):
            return match.group(0)  # filenames, not secrets
        changed = True
        return f"{name}={quote}{mask}{quote}"

    out = _ASSIGNMENT_VALUE_RE.sub(_replace, text)
    return out, changed


class Sanitizer:
    """Redacts known secret values (literal) and pattern/high-entropy secrets
    (via SecretShield). Use for every string before display or persistence."""

    def __init__(
        self,
        *,
        extra_secrets: Iterable[str] = (),
        mask: str = "********",
        entropy_threshold: float = 4.2,
        enabled: bool = True,
        minimum_literal_length: int = 6,
    ) -> None:
        self.mask = mask
        self.entropy_threshold = entropy_threshold
        self.enabled = enabled
        self.minimum_literal_length = minimum_literal_length
        known = collect_environment_secrets(
            extra=extra_secrets, minimum_length=minimum_literal_length
        )
        self._known_secrets: list[str] = sorted(set(known), key=len, reverse=True)

    def register_secret(self, value: str) -> None:
        """Register another known secret value (e.g. the GitHub token)."""
        if value and len(value) >= self.minimum_literal_length and value not in self._known_secrets:
            self._known_secrets.append(value)
            self._known_secrets.sort(key=len, reverse=True)

    @property
    def known_secret_count(self) -> int:
        return len(self._known_secrets)

    def redact(self, text: str) -> str:
        """Redact known literals, then secret-looking assignments, then let
        SecretShield's pattern + entropy detectors finish the job."""
        if not self.enabled or not text:
            return text
        literal, _ = _literal_redact(text, self._known_secrets, self.mask)
        assigned, _ = _mask_secret_assignments(literal, self.mask)
        out, _was = secretshield.redact(
            assigned,
            entropy_threshold=self.entropy_threshold,
            redact_with=self.mask,
        )
        return out

    def redact_mapping(self, mapping: Mapping[str, str]) -> dict[str, str]:
        """Redact every *value* of a mapping, leaving keys intact."""
        return {key: self.redact(str(value)) for key, value in mapping.items()}

    def describe(self, text: str) -> str:
        """Human-readable summary: how many secret spans were found."""
        if not text:
            return "no secrets detected"
        return self.redact(text)

    def scan(self, text: str) -> list[dict]:
        """Report detected spans (kind/positions). Secret *values* are never
        included in the returned records."""
        findings: list[dict] = []
        if not self.enabled or not text:
            return findings
        for secret in self._known_secrets:
            start = 0
            while True:
                idx = text.find(secret, start)
                if idx < 0:
                    break
                findings.append({"kind": "known_value", "start": idx, "end": idx + len(secret)})
                start = idx + len(secret)
        try:
            matches = secretshield.detect(text, entropy_threshold=self.entropy_threshold)
        except Exception:  # noqa: BLE001 - detection must never crash the CLI
            return findings
        for match in matches:
            findings.append({"kind": match.kind, "start": match.start, "end": match.end})
        # Merge overlapping spans (simple, ordered).
        findings.sort(key=lambda f: (f["start"], f["end"]))
        merged: list[dict] = []
        for finding in findings:
            if merged and finding["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], finding["end"])
            else:
                merged.append(dict(finding))
        return merged


def enable_global_protection(sanitizer: Sanitizer) -> None:
    """Turn on SecretShield's stream/log protection plus a literal-value
    filter for the ``replico`` logger.

    Debug and verbose output must respect redaction, so this is called from
    the CLI entry point whenever the security layer is enabled.
    """
    try:
        secretshield.configure(
            enabled=True,
            notify=False,
            entropy_threshold=sanitizer.entropy_threshold,
            redact_with=sanitizer.mask,
        )
        secretshield.enable()
    except Exception:  # noqa: BLE001
        # The library is a hard dependency; if its runtime hook ever fails we
        # degrade gracefully rather than crash the CLI.
        return
    _install_logging_filter(sanitizer)


def _install_logging_filter(sanitizer: Sanitizer) -> None:
    import logging  # local import keeps module import cheap

    class _RedactingFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                if record.msg and isinstance(record.msg, str):
                    record.msg = sanitizer.redact(record.msg)
                if record.args:
                    record.args = tuple(
                        sanitizer.redact(str(a)) if isinstance(a, str) else a for a in record.args
                    )
            except Exception:  # noqa: BLE001
                pass
            return True

    for name in ("replico",):
        logger = logging.getLogger(name)
        logger.addFilter(_RedactingFilter())
