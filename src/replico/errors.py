"""Stable exit codes and the Replico exception hierarchy.

Exit code contract (documented in README.md):

    0   reproduction succeeded — the tool ran cleanly and the CI failure did
        NOT reappear locally (verdict ``not_reproduced``, or a ``rerun`` that
        now passes). Nothing is broken locally.
    1   reproduced failure still exists — the CI failure WAS reproduced
        locally (verdict ``reproduced``), or a ``rerun`` still fails.
    2   could not reproduce — no trustworthy verdict was reached
        (``partially_reproduced`` without a local failure, or the user
        declined a required confirmation).
    3   invalid input — bad URL, unknown job, missing arguments, ... .
    4   authentication problem — a private repository could not be read and
        no usable token is available.
    5   unsupported workflow — Replico does not yet know how to reproduce
        this job/step/ecosystem.
    6   environment/setup problem — a required local tool is missing or a
        setup step (venv, install, Docker) failed.
    70  internal error — an unexpected bug; please report it.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE_EXISTS = 1
EXIT_COULD_NOT_REPRODUCE = 2
EXIT_INVALID_INPUT = 3
EXIT_AUTH = 4
EXIT_UNSUPPORTED = 5
EXIT_ENVIRONMENT = 6
EXIT_INTERNAL = 70

EXIT_NAMES = {
    EXIT_OK: "reproduction succeeded (no failure reproduced locally)",
    EXIT_FAILURE_EXISTS: "reproduced failure still exists",
    EXIT_COULD_NOT_REPRODUCE: "could not reproduce",
    EXIT_INVALID_INPUT: "invalid input",
    EXIT_AUTH: "authentication problem",
    EXIT_UNSUPPORTED: "unsupported workflow",
    EXIT_ENVIRONMENT: "environment/setup problem",
    EXIT_INTERNAL: "internal error",
}


class ReplicoError(Exception):
    """Base class for all expected Replico failures."""

    exit_code = EXIT_INTERNAL

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class InvalidInputError(ReplicoError):
    exit_code = EXIT_INVALID_INPUT


class AuthError(ReplicoError):
    exit_code = EXIT_AUTH


class UnsupportedError(ReplicoError):
    exit_code = EXIT_UNSUPPORTED


class SetupError(ReplicoError):
    exit_code = EXIT_ENVIRONMENT


class ReproNotPossibleError(ReplicoError):
    """A reproduction could not be completed to a verdict.

    Used when the user declines a required confirmation or when the local
    state prevents a trustworthy run.
    """

    exit_code = EXIT_COULD_NOT_REPRODUCE
