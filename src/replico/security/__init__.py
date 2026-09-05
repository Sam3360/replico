"""Security package.

The security layer is the single gateway between Replico and anything that
could contain a secret. Everything that is displayed or persisted passes
through :class:`replico.security.redaction.Sanitizer`.
"""

from replico.security.guard import (
    RiskFinding,
    audit_command_text,
    audit_environment,
    safe_join,
    validate_env_name,
)
from replico.security.redaction import (
    SECRET_ENV_NAME_RE,
    Sanitizer,
    collect_environment_secrets,
    describe_env_var,
    is_sensitive_env_name,
)

__all__ = [
    "SECRET_ENV_NAME_RE",
    "RiskFinding",
    "Sanitizer",
    "audit_command_text",
    "audit_environment",
    "collect_environment_secrets",
    "describe_env_var",
    "is_sensitive_env_name",
    "safe_join",
    "validate_env_name",
]
