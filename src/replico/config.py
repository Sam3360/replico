"""Replico configuration: defaults <- .replico.toml <- environment.

Basic usage requires no configuration at all. When present, the file is
searched upward from the current directory (or from ``$REPLICO_CONFIG``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from replico.errors import InvalidInputError
from replico.util import find_upwards

CONFIG_FILE = ".replico.toml"


@dataclass
class GitHubConfig:
    token_env: str = "GITHUB_TOKEN"
    api_base: str = "https://api.github.com"
    host: str = "github.com"  # for building html/raw URLs
    timeout_s: int = 60
    log_max_bytes: int = 6 * 1024 * 1024


@dataclass
class ExecutionConfig:
    mode: str = "auto"  # auto | docker | local
    prefer_venv: bool = True
    install_timeout_s: int = 900
    command_timeout_s: int = 3600
    default_shell_timeout_s: int = 1800


@dataclass
class PythonConfig:
    preferred_version: str = "auto"  # auto | exact version like "3.13"
    allow_mismatch: bool = True
    venv_dir: str = ".replico/venv"


@dataclass
class SecurityConfig:
    redact_secrets: bool = True
    notify: bool = False
    entropy_threshold: float = 4.2
    mask_with: str = "********"
    disable_telemetry: bool = True  # informational: replico has none


@dataclass
class DiagnosticsConfig:
    """Whether to diagnose reproduced failures with WhyFail.

    These switches only ever *disable* diagnosis; they cannot disable secret
    redaction (that stays mandatory in the security layer).
    """

    enabled: bool = True
    whyfail: bool = True


@dataclass
class OutputConfig:
    verbose: bool = False
    plain: bool = False
    debug: bool = False


@dataclass
class Config:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    python: PythonConfig = field(default_factory=PythonConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    config_path: Path | None = None


def _find_config_file(cwd: Path | None = None) -> Path | None:
    explicit = os.environ.get("REPLICO_CONFIG")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise InvalidInputError(f"REPLICO_CONFIG points to a missing file: {path}")
        return path
    return find_upwards(CONFIG_FILE, cwd)


def _coerce(section: str, data: dict) -> dict:
    """Drop unknown keys so future config files stay forward compatible."""
    known = {
        "github": {"token_env", "api_base", "host", "timeout_s", "log_max_bytes"},
        "execution": {
            "mode",
            "prefer_venv",
            "install_timeout_s",
            "command_timeout_s",
            "default_shell_timeout_s",
        },
        "python": {"preferred_version", "allow_mismatch", "venv_dir"},
        "security": {
            "redact_secrets",
            "notify",
            "entropy_threshold",
            "mask_with",
            "disable_telemetry",
        },
        "diagnostics": {"enabled", "whyfail"},
        "output": {"verbose", "plain", "debug"},
    }[section]
    return {key: value for key, value in data.items() if key in known}


def load_config(cwd: Path | None = None) -> Config:
    """Load effective configuration (defaults + optional TOML file + env)."""
    cfg = Config()
    github = cfg.github
    execution = cfg.execution
    python = cfg.python
    security = cfg.security
    diagnostics = cfg.diagnostics
    output = cfg.output
    path = _find_config_file(cwd)

    if path is not None:
        try:
            raw = path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise InvalidInputError(f"invalid config file {path}: {exc}") from exc
        except OSError as exc:
            raise InvalidInputError(f"cannot read config file {path}: {exc}") from exc

        github = replace(github, **_coerce("github", data.get("github", {})))
        if not github.api_base.startswith(("https://", "http://")):
            raise InvalidInputError(f"invalid api_base in {path}: {github.api_base!r}")
        if github.token_env:
            github.token_env = github.token_env.strip() or "GITHUB_TOKEN"
        execution = replace(execution, **_coerce("execution", data.get("execution", {})))
        if execution.mode not in ("auto", "docker", "local"):
            raise InvalidInputError(
                f"invalid execution.mode {execution.mode!r} in {path} (expected auto|docker|local)"
            )
        python = replace(python, **_coerce("python", data.get("python", {})))
        security = replace(security, **_coerce("security", data.get("security", {})))
        diagnostics = replace(diagnostics, **_coerce("diagnostics", data.get("diagnostics", {})))
        output = replace(output, **_coerce("output", data.get("output", {})))

    # Environment variable overrides apply even without a config file
    # (never secrets; only switches).
    env_mode = os.environ.get("REPLICO_EXECUTION_MODE")
    if env_mode:
        if env_mode not in ("auto", "docker", "local"):
            raise InvalidInputError(
                f"invalid REPLICO_EXECUTION_MODE {env_mode!r} (expected auto|docker|local)"
            )
        execution.mode = env_mode
    if os.environ.get("REPLICO_VERBOSE") and not output.verbose:
        output.verbose = True

    return Config(
        github=github,
        execution=execution,
        python=python,
        security=security,
        diagnostics=diagnostics,
        output=output,
        config_path=path,
    )


def config_to_mapping(cfg: Config) -> dict:
    """Serializable, secret-free view used by `replico config` and --json."""
    return {
        "github": {
            "token_env": cfg.github.token_env,
            "api_base": cfg.github.api_base,
            "host": cfg.github.host,
            "timeout_s": cfg.github.timeout_s,
            "log_max_bytes": cfg.github.log_max_bytes,
        },
        "execution": {
            "mode": cfg.execution.mode,
            "prefer_venv": cfg.execution.prefer_venv,
            "install_timeout_s": cfg.execution.install_timeout_s,
            "command_timeout_s": cfg.execution.command_timeout_s,
        },
        "python": {
            "preferred_version": cfg.python.preferred_version,
            "allow_mismatch": cfg.python.allow_mismatch,
            "venv_dir": cfg.python.venv_dir,
        },
        "security": {
            "redact_secrets": cfg.security.redact_secrets,
            "notify": cfg.security.notify,
            "entropy_threshold": cfg.security.entropy_threshold,
            "disable_telemetry": cfg.security.disable_telemetry,
        },
        "diagnostics": {
            "enabled": cfg.diagnostics.enabled,
            "whyfail": cfg.diagnostics.whyfail,
        },
        "output": {"verbose": cfg.output.verbose, "plain": cfg.output.plain},
        "config_path": str(cfg.config_path) if cfg.config_path else None,
    }
