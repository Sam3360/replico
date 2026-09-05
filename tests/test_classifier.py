"""Evidence-based classification tests."""

from __future__ import annotations

from replico.analysis import classifier as clsf
from replico.analysis.logs import analyze_log
from replico.models import FailureEvidence


def classify_text(text: str) -> clsf.FailureClassification:
    return clsf.classify(analyze_log(text))


def test_pytest_failure_classified():
    c = classify_text("FAILED tests/test_auth.py::test_login - AssertionError: 401")
    assert c.category == clsf.TEST_FAILURE
    assert c.confidence >= 90
    assert c.explanation


def test_module_not_found_classified():
    c = classify_text("ModuleNotFoundError: No module named 'requests'")
    assert c.category == clsf.DEPENDENCY_FAILURE


def test_pip_resolution_classified():
    c = classify_text(
        "ERROR: Cannot install requests==2.0 and urllib3 because these package versions have conflicting dependencies"
    )
    assert c.category == clsf.DEPENDENCY_FAILURE


def test_missing_executable_classified():
    c = classify_text("bash: cmake: command not found")
    assert c.category == clsf.MISSING_TOOL


def test_timeout_classified():
    c = classify_text("The operation was canceled")
    assert c.category == clsf.TIMEOUT


def test_env_var_keyerror_classified():
    c = classify_text("KeyError: 'API_TOKEN'")
    assert c.category == clsf.ENVIRONMENT_VARIABLE


def test_permission_classified():
    c = classify_text("Permission denied (publickey)")
    assert c.category == clsf.PERMISSION_FAILURE


def test_unknown_stays_unknown():
    c = classify_text("gobbledygook happened")
    assert c.category == clsf.UNKNOWN
    assert c.confidence < 40


def test_unknown_explanation_is_honest():
    c = classify_text("gobbledygook")
    assert "cannot" in c.explanation[0].lower()


def test_signature_prefers_tests():
    a = analyze_log("FAILED tests/a.py::b - boom")
    b = analyze_log("FAILED tests/a.py::b - boom different detail")
    assert clsf.signature(a) == clsf.signature(b)


def test_signature_distinguishes_different_tests():
    a = analyze_log("FAILED tests/a.py::b - boom")
    c = analyze_log("FAILED tests/c.py::d - boom")
    assert clsf.signature(a) != clsf.signature(c)


def test_empty_evidence_not_classified_high():
    c = clsf.classify(FailureEvidence())
    assert c.category == clsf.UNKNOWN or c.confidence <= 40
