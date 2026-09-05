"""Configuration tests."""

from __future__ import annotations

import pytest

from replico.config import config_to_mapping, load_config
from replico.errors import InvalidInputError


def test_defaults_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.execution.mode == "auto"
    assert cfg.github.token_env == "GITHUB_TOKEN"
    assert cfg.security.redact_secrets is True
    assert cfg.python.allow_mismatch is True


def test_config_file_overrides(tmp_path, monkeypatch):
    (tmp_path / ".replico.toml").write_text(
        """
[execution]
mode = "local"
install_timeout_s = 30

[python]
preferred_version = "3.12"
allow_mismatch = false

[security]
redact_secrets = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.execution.mode == "local"
    assert cfg.python.preferred_version == "3.12"
    assert cfg.python.allow_mismatch is False
    assert cfg.config_path == tmp_path / ".replico.toml"


def test_found_upwards_from_subdir(tmp_path, monkeypatch):
    (tmp_path / ".replico.toml").write_text("[output]\nverbose = true\n", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    cfg = load_config()
    assert cfg.output.verbose is True


def test_invalid_mode_rejected(tmp_path, monkeypatch):
    (tmp_path / ".replico.toml").write_text('[execution]\nmode = "cloud"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InvalidInputError):
        load_config()


def test_invalid_toml_rejected(tmp_path, monkeypatch):
    (tmp_path / ".replico.toml").write_text("not [ valid toml", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InvalidInputError):
        load_config()


def test_unknown_keys_ignored(tmp_path, monkeypatch):
    (tmp_path / ".replico.toml").write_text(
        "[output]\nverbose = true\n[future]\nsection = 1\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.output.verbose is True


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPLICO_EXECUTION_MODE", "docker")
    assert load_config().execution.mode == "docker"


def test_mapping_has_no_secrets():
    from replico.config import Config

    mapping = config_to_mapping(Config())
    blob = str(mapping)
    assert "token" not in blob.lower() or "token_env" in blob.lower()
    assert mapping["github"]["token_env"] == "GITHUB_TOKEN"
