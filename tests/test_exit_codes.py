"""The exit-code contract is a stable public API — lock it down."""

from __future__ import annotations

from replico.errors import (
    EXIT_AUTH,
    EXIT_COULD_NOT_REPRODUCE,
    EXIT_ENVIRONMENT,
    EXIT_FAILURE_EXISTS,
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_UNSUPPORTED,
    AuthError,
    InvalidInputError,
    ReproNotPossibleError,
    SetupError,
    UnsupportedError,
)
from replico.models import Verdict
from replico.pipeline import _outcome_for_verdict


def test_exit_code_constants_stable():
    assert EXIT_OK == 0
    assert EXIT_FAILURE_EXISTS == 1
    assert EXIT_COULD_NOT_REPRODUCE == 2
    assert EXIT_INVALID_INPUT == 3
    assert EXIT_AUTH == 4
    assert EXIT_UNSUPPORTED == 5
    assert EXIT_ENVIRONMENT == 6
    assert EXIT_INTERNAL == 70


def test_exception_to_exit_code():
    assert InvalidInputError("x").exit_code == 3
    assert AuthError("x").exit_code == 4
    assert UnsupportedError("x").exit_code == 5
    assert SetupError("x").exit_code == 6
    assert ReproNotPossibleError("x").exit_code == 2


def test_verdict_to_exit_code():
    assert _outcome_for_verdict(Verdict.REPRODUCED) == 1
    assert _outcome_for_verdict(Verdict.NOT_REPRODUCED) == 0
    assert _outcome_for_verdict(Verdict.PARTIALLY_REPRODUCED) == 2
    assert _outcome_for_verdict(Verdict.UNSUPPORTED) == 5


def test_exceptions_carry_hints():
    error = InvalidInputError("bad", hint="do this instead")
    assert error.hint == "do this instead"
    assert error.message == "bad"
