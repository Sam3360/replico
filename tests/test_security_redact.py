"""Security tests: SecretShield integration, literal redaction, env handling.

These are the regression tests for the no-leakage contract: whatever goes in
through Replico's display/persistence paths comes out redacted.
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import suppress

import pytest

from replico.security.redaction import (
    Sanitizer,
    collect_environment_secrets,
    describe_env_var,
    enable_global_protection,
    is_sensitive_env_name,
)

GHP_TOKEN = "ghp_" + "A" * 36
AWS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpQIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


def test_secretshield_pattern_tokens_redacted():
    sanitizer = Sanitizer()
    text = f"token={GHP_TOKEN} and key={AWS_KEY}"
    out = sanitizer.redact(text)
    assert GHP_TOKEN not in out and AWS_KEY not in out
    assert "token=" in out and "key=" in out


def test_private_key_redacted():
    sanitizer = Sanitizer()
    out = sanitizer.redact(PEM)
    assert "PRIVATE KEY" not in out


def test_plain_text_untouched():
    sanitizer = Sanitizer()
    text = "build succeeded, all 42 tests passed, nothing secret here"
    assert sanitizer.redact(text) == text


def test_known_literal_secret_redacted():
    # SecretShield is pattern/entropy based; Replico adds known-value redaction.
    secret = "purplemonkeydishwasher42"
    sanitizer = Sanitizer(extra_secrets=[secret])
    text = f"DATABASE_PASSWORD={secret} connection ok"
    out = sanitizer.redact(text)
    assert secret not in out


def test_short_literal_not_mangled():
    sanitizer = Sanitizer(extra_secrets=["abc"])
    text = "abcdef"
    assert sanitizer.redact(text) == text  # too short to safely replace


def test_register_secret_runtime():
    sanitizer = Sanitizer()
    sanitizer.register_secret("runtimevalue-xyz-123456")
    out = sanitizer.redact("runtimevalue-xyz-123456 leaked")
    assert "runtimevalue-xyz-123456" not in out


def test_scan_reports_kinds_without_values():
    sanitizer = Sanitizer(extra_secrets=["literal-123456"])
    findings = sanitizer.scan(f"x {GHP_TOKEN} {AWS_KEY} literal-123456")
    kinds = {f["kind"] for f in findings}
    assert kinds  # at least one finding
    assert all(f["start"] < f["end"] for f in findings)
    blob = json.dumps(findings)
    assert GHP_TOKEN not in blob and AWS_KEY not in blob and "literal-123456" not in blob


def test_redact_mapping_values_only():
    sanitizer = Sanitizer(extra_secrets=["sekrit-999"])
    out = sanitizer.redact_mapping({"TOKEN": "sekrit-999", "PATH": "/usr/bin"})
    assert out["TOKEN"] != "sekrit-999"
    assert out["PATH"] == "/usr/bin"


def test_environment_secret_collection():
    secret = "env-secret-" + secrets.token_hex(8)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MY_API_TOKEN", secret)
        mp.setenv("PLAIN_VALUE", "public-value")
        values = collect_environment_secrets(minimum_length=6)
    assert secret in values
    assert "public-value" not in values


def test_sensitive_name_classification():
    assert is_sensitive_env_name("GITHUB_TOKEN")
    assert is_sensitive_env_name("AWS_SECRET_ACCESS_KEY")
    assert is_sensitive_env_name("my_password")
    assert is_sensitive_env_name("DB_PASSWORD")
    assert not is_sensitive_env_name("PATH")
    assert not is_sensitive_env_name("PWD")
    assert not is_sensitive_env_name("USERPROFILE")


def test_describe_env_var_never_shows_value():
    assert describe_env_var("API_KEY", "abc123") == "API_KEY = present"
    assert describe_env_var("API_KEY", None) == "API_KEY = absent"


def test_enable_global_protection_runs_and_redacts_via_direct_call(capsys):
    # We cannot rely on capsys for a wrapper installed around the real stream,
    # but we can verify the call succeeds and the sanitizer remains functional.
    sanitizer = Sanitizer(extra_secrets=["zz-extra-123456"])
    enable_global_protection(sanitizer)
    try:
        out = sanitizer.redact(f"secret here zz-extra-123456 and {GHP_TOKEN}")
        assert "zz-extra-123456" not in out
        assert GHP_TOKEN not in out
    finally:
        import secretshield

        with suppress(Exception):
            secretshield.disable()


def test_no_leakage_through_debug_json_env_values(monkeypatch):
    secret = "super-secret-" + secrets.token_hex(6)
    monkeypatch.setenv("SOME_TOKEN", secret)
    sanitizer = Sanitizer()
    blob = json.dumps({"debug_env": {"SOME_TOKEN": secret, "PATH": os.environ.get("PATH", "")}})
    out = sanitizer.redact(blob)
    assert secret not in out
    assert "PATH" in out
