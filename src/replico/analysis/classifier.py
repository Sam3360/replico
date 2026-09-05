"""Evidence-based failure classification.

Classification is a set of ordered rules over observed evidence. Replico
never invents an explanation: when nothing matches, the category is UNKNOWN
with a low confidence and a note saying so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from replico.models import FailureEvidence

# Failure categories (stable identifiers, documented in README).
DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
VERSION_FAILURE = "VERSION_FAILURE"
PYTHON_VERSION_FAILURE = "PYTHON_VERSION_FAILURE"
NODE_VERSION_FAILURE = "NODE_VERSION_FAILURE"
OS_DIFFERENCE = "OS_DIFFERENCE"
ENVIRONMENT_VARIABLE = "ENVIRONMENT_VARIABLE"
MISSING_TOOL = "MISSING_TOOL"
MISSING_FILE = "MISSING_FILE"
TEST_FAILURE = "TEST_FAILURE"
BUILD_FAILURE = "BUILD_FAILURE"
NETWORK_FAILURE = "NETWORK_FAILURE"
TIMEOUT = "TIMEOUT"
PERMISSION_FAILURE = "PERMISSION_FAILURE"
WORKFLOW_CONFIGURATION = "WORKFLOW_CONFIGURATION"
PYTHON_EXCEPTION = "PYTHON_EXCEPTION"
AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
WORKFLOW_PARSE_ERROR = "WORKFLOW_PARSE_ERROR"
COMMAND_FAILURE = "COMMAND_FAILURE"
INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
UNKNOWN = "UNKNOWN"

CATEGORY_NAMES = {
    DEPENDENCY_FAILURE: "dependency failure",
    VERSION_FAILURE: "version failure",
    PYTHON_VERSION_FAILURE: "Python version failure",
    NODE_VERSION_FAILURE: "Node version failure",
    OS_DIFFERENCE: "OS difference",
    ENVIRONMENT_VARIABLE: "missing/different environment variable",
    MISSING_TOOL: "missing tool/executable",
    MISSING_FILE: "missing file",
    TEST_FAILURE: "test failure",
    BUILD_FAILURE: "build failure",
    NETWORK_FAILURE: "network failure",
    TIMEOUT: "timeout",
    PERMISSION_FAILURE: "permission failure",
    WORKFLOW_CONFIGURATION: "workflow configuration",
    PYTHON_EXCEPTION: "Python exception",
    AUTHENTICATION_REQUIRED: "authentication required",
    UNSUPPORTED_ACTION: "unsupported action",
    WORKFLOW_PARSE_ERROR: "workflow parse error",
    COMMAND_FAILURE: "command failure",
    INFRASTRUCTURE_FAILURE: "infrastructure failure",
    UNKNOWN: "unknown",
}


@dataclass
class FailureClassification:
    category: str = UNKNOWN
    confidence: int = 0  # 0..100
    explanation: list[str] = field(default_factory=list)
    kind: str = ""
    # When WhyFail diagnosed this failure, its structured diagnostic dict is
    # attached here (always redacted). Never claimed without real evidence.
    whyfail: dict | None = None

    def to_mapping(self) -> dict:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "kind": self.kind,
            "whyfail": self.whyfail,
        }


@dataclass
class _Rule:
    name: str
    patterns: list[re.Pattern]
    category: str
    confidence: int
    explanation: str


_RULES: list[_Rule] = []


def _rule(name, category, confidence, explanation, *patterns):
    _RULES.append(
        _Rule(
            name=name,
            # MULTILINE makes line-anchored patterns (^Killed$, ^...Error$)
            # work against the multi-line haystack built from log windows.
            patterns=[re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns],
            category=category,
            confidence=confidence,
            explanation=explanation,
        )
    )


_rule(
    "python_missing_package",
    DEPENDENCY_FAILURE,
    90,
    "Python reported a missing module, which usually means CI installed a "
    "dependency that is not present (or not installed) locally.",
    r"ModuleNotFoundError:\s*No module named '([^']+)'",
    r"ImportError:\s*No module named",
    r"Could not import",
)
_rule(
    "pip_resolution",
    DEPENDENCY_FAILURE,
    92,
    "The package resolver could not find a satisfying set of versions; CI and "
    "local dependency graphs are likely out of sync.",
    r"ResolutionImpossible",
    r"No matching distribution found",
    r"Could not find a version that satisfies",
    r"pip.*(?:conflict|resolution|incompatible)",
    r"ERROR: Cannot install",
)
_rule(
    "pytest_failure",
    TEST_FAILURE,
    94,
    "A pytest test failed in CI. Compare the failing test id with the local run "
    "to confirm the same test fails locally.",
    r"FAILED\s+\S+::\S+",
    r"short test summary info",
)
_rule(
    "assertion",
    TEST_FAILURE,
    85,
    "An assertion failed during execution — behavior differs between CI and the local environment.",
    r"AssertionError",
    r"assert\s",
)
_rule(
    "python_version_required",
    PYTHON_VERSION_FAILURE,
    88,
    "The toolchain reported a Python interpreter requirement that is not met.",
    r"requires Python >=? ?\d+\.\d+",
    r"Python \d+\.\d+.*(?:required|needed)",
    r"not supported.*python",
    r"RuntimeError: Python \d+\.\d+",
)
_rule(
    "node_version_required",
    NODE_VERSION_FAILURE,
    88,
    "Node reported an engine/version requirement that is not satisfied.",
    r"Unsupported engine",
    r"requires node >=? ?\d+",
    r"engine \"node\"",
    r"ENOTSUP",
    r"v\d+\.\d+\.\d+.*not supported|not supported.*node",
)
_rule(
    "missing_executable",
    MISSING_TOOL,
    88,
    "A tool invoked by the workflow is not installed in the environment.",
    r"(?:command not found|is not recognized|not recognized as an? internal)",
    r"execvp\(.*No such file",
    r"The process ['\"]?[^'\"]+['\"]? failed with exit code 127",
    r"FileNotFoundError: \[Errno 2\].*No such file or directory: '([^']+)'",
)
_rule(
    "missing_file",
    MISSING_FILE,
    86,
    "A file referenced by the workflow does not exist in the repository or "
    "was not produced before the failing step.",
    r"No such file or directory",
    r"cannot open file ['\"]?([^'\"]+)['\"]?",
    r"error: pathspec ['\"]?[^'\"]+['\"]? did not match",
)
_rule(
    "timeout",
    TIMEOUT,
    92,
    "The run exceeded a time budget (step or job timeout).",
    r"timed out|timed?out",
    r"The operation was canceled",
    r"##\[error\]The operation was canceled",
    r"exit code 124",
)
_rule(
    "network",
    NETWORK_FAILURE,
    86,
    "A network operation failed (download, package registry, DNS).",
    r"Could not resolve host|Temporary failure in name resolution",
    r"Connection (?:refused|reset|timed out)",
    r"Failed to (?:download|fetch|connect)",
    r"ssl: (?:wrong version number|certificate verify failed)",
    r"getaddrinfo failed",
    r"EAI_AGAIN",
)
_rule(
    "permission",
    PERMISSION_FAILURE,
    88,
    "An operation failed because of missing permissions.",
    r"Permission denied",
    r"PermissionError",
    r"EACCES|EPERM",
    r"Access is denied",
    r"denied \(publickey\)",
)
_rule(
    "build_compile",
    BUILD_FAILURE,
    84,
    "Compilation or a language build step failed.",
    r"(^|[\s'\"])error(\[E\d+\])?:[^\n]*",
    r"fatal error:",
    r"error: linking",
    r"Build FAILED",
    r"gcc: error",
    r"cargo build.*failed",
)
_rule(
    "env_missing",
    ENVIRONMENT_VARIABLE,
    85,
    "An environment variable that the code expects was not provided.",
    r"KeyError: ['\"]([A-Z_]+)['\"]",
    r"Environment variable ['\"]?([A-Z_]+)['\"]? (?:is|was) not (?:set|found)",
    r"os\.environ\[[^\]]+\].*KeyError",
    r"Missing environment variable",
)
_rule(
    "workflow_config",
    WORKFLOW_CONFIGURATION,
    80,
    "The workflow itself is invalid or references something that does not exist.",
    r"Workflow does not have|Invalid workflow file",
    r"Unable to resolve action",
    r"Error: .*action.*not found",
    r"Could not find action",
)
_rule(
    "segfault_crash",
    BUILD_FAILURE,
    80,
    "The process crashed (segfault / killed), often due to an environment "
    "difference such as compiler flags or memory limits.",
    r"Segmentation fault",
    r"^Killed$",
)
_rule(
    "docker_missing",
    MISSING_TOOL,
    90,
    "Docker is required by the workflow but was not available in the runner.",
    r"docker: (?:command not found|Cannot connect)",
    r"Cannot connect to the Docker daemon",
)
_rule(
    "git_missing",
    MISSING_TOOL,
    88,
    "git reported a problem resolving the requested ref.",
    r"fatal: (?:not a git repository|repository .* not found|ambiguous argument)",
)
_rule(
    "python_exception_traceback",
    PYTHON_EXCEPTION,
    78,
    "The failing step raised a Python exception. When the failure is "
    "reproduced locally, WhyFail can diagnose the exception from local "
    "runtime evidence.",
    r"Traceback \(most recent call last\)",
    r"^\s*(?:[A-Za-z_][\w.]*\.)*[A-Za-z_][\w]*Error[^\n]*$",
)
_rule(
    "python_exception_keyerror",
    PYTHON_EXCEPTION,
    76,
    "A Python exception was raised (missing key or attribute). WhyFail can "
    "diagnose it from the locally reproduced runtime evidence.",
    r"KeyError: \S+",
    r"AttributeError: \S+",
)
_rule(
    "python_exception_type",
    PYTHON_EXCEPTION,
    74,
    "A Python exception was raised during the failing step.",
    r"TypeError: \S+",
    r"ValueError: \S+",
    r"IndexError: \S+",
    r"NameError: \S+",
    r"UnboundLocalError: \S+",
    r"ZeroDivisionError: \S+",
    r"RuntimeError: \S+",
)
_rule(
    "authentication_required",
    AUTHENTICATION_REQUIRED,
    88,
    "GitHub (or a dependency source) demanded authentication that is not "
    "available — set GITHUB_TOKEN or run `gh auth login`.",
    r"API rate limit exceeded",
    r"Bad credentials",
    r"Requires authentication",
    r"Authentication failed",
    r"401 Unauthorized",
    r"403 Forbidden.*(?:token|auth)",
    r"not authorized",
    r"Could not authenticate",
)
_rule(
    "unsupported_action",
    UNSUPPORTED_ACTION,
    82,
    "The workflow relies on an action or capability Replico cannot replay "
    "locally; the failure itself may be specific to that action.",
    r"Unable to resolve action",
    r"Error: .*action.*not found",
    r"Could not find action",
    r"Node \d+ is not installed",
)
_rule(
    "workflow_parse_error",
    WORKFLOW_PARSE_ERROR,
    85,
    "The workflow file itself is malformed or references an undefined job/step.",
    r"Invalid workflow file",
    r"Workflow does not have",
    r"You have an error in your yaml syntax",
    r"Unexpected value .* in workflow",
    r"Unrecognized named-value: 'env'",
)
_rule(
    "command_failure",
    COMMAND_FAILURE,
    86,
    "The step command failed without a more specific signal.",
    r"Error: Process completed with exit code \d+",
    r"The process .* failed with exit code",
    r"##\[error\]Process completed",
)
_rule(
    "infrastructure_failure",
    INFRASTRUCTURE_FAILURE,
    78,
    "The runner or infrastructure failed (crash, resource limits, daemon "
    "problems) rather than the project code.",
    r"Segmentation fault",
    r"^Killed$",
    r"Out of memory|Killed due to memory",
    r"Cannot connect to the Docker daemon",
    r"The self-hosted runner: .* lost communication",
    r"runner .* has gone offline",
)


def classify(evidence: FailureEvidence) -> FailureClassification:
    """Classify CI (or local) failure evidence using ordered rules."""
    haystack = "\n".join(evidence.lines)
    if evidence.summary:
        haystack = f"{haystack}\n{evidence.summary}"

    best: FailureClassification | None = None
    candidates: list[FailureClassification] = []
    for rule in _RULES:
        for pattern in rule.patterns:
            match = pattern.search(haystack)
            if match:
                detail = ""
                groups = [g for g in match.groups() if g]
                if groups:
                    detail = f" — {groups[0]}"
                candidates.append(
                    FailureClassification(
                        category=rule.category,
                        confidence=rule.confidence,
                        kind=rule.name,
                        explanation=[f"{rule.explanation}{detail}"],
                    )
                )
                break
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    for candidate in candidates:
        # A bare KeyError on a lowercase key is a Python data-structure
        # failure (dict access), not a missing environment variable — only
        # all-caps keys (or an os.environ context) count as env failures.
        if candidate.kind == "env_missing":
            key_match = re.search(r"KeyError:\s*['\"]([^'\"]+)['\"]", haystack)
            if key_match and not key_match.group(1).isupper():
                continue
        best = candidate
        break
    if best is None:
        return FailureClassification(
            category=UNKNOWN,
            confidence=25,
            explanation=[
                "Replico cannot map the log output to a known failure "
                "category; treat any reproduction claim with care."
            ],
        )
    # clamp confidence when the summary did not actually match much text
    if len(evidence.lines) == 0:
        best.confidence = min(best.confidence, 40)
    return best


def signature(evidence: FailureEvidence) -> str:
    """A stable signature identifying *this* failure.

    Prefers failing test ids; otherwise uses category + a normalized summary
    prefix so identical errors compare equal while versions/dates do not.
    """
    if evidence.failing_tests:
        return "tests:" + ",".join(sorted(evidence.failing_tests))
    kind = evidence.category_hint or "?"
    summary = re.sub(r"[0-9]+\.[0-9]+", "X.Y", evidence.summary)
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", summary).strip().lower()[:80]
    return f"{kind}:{normalized or 'none'}"
