"""CLI tests: invoke the real entry point (offline)."""

from __future__ import annotations

import json

from replico.cli import main, rep_main


def test_version(capsys):
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("replico ")


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith("replico ")


def test_rep_alias(capsys):
    """`rep` is a real console-script alias of `replico`."""
    assert rep_main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("replico ")
    assert rep_main(["version"]) == 0
    assert capsys.readouterr().out.startswith("replico ")


def test_help(capsys):
    assert main(["help"]) == 0
    assert "reproduce" in capsys.readouterr().out


def test_bare_help(capsys):
    assert main([]) == 0
    assert "reproduce" in capsys.readouterr().out


def test_env_json_is_valid_and_has_no_values(capsys):
    assert main(["env", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "env"
    assert "os" in doc


def test_env_plain_hides_secret_values(capsys, monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live_xyz123456789")
    assert main(["env", "--plain"]) == 0
    out = capsys.readouterr().out
    assert "sk_live_xyz123456789" not in out
    assert "STRIPE_API_KEY = present" in out


def test_missing_target_exit_3(capsys):
    assert main(["reproduce"]) == 3
    assert "missing run" in capsys.readouterr().out


def test_invalid_url_exit_3(capsys):
    assert main(["https://example.com/not-a-run"]) == 3


def test_url_without_checkout_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # not a git checkout
    code = main(["https://github.com/o/r/actions/runs/5", "--plain"])
    assert code == 3
    assert "checkout" in capsys.readouterr().out


def test_status_without_store_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["status", "--plain"]) == 3
    assert "no saved reproduction" in capsys.readouterr().out


def test_error_json_document(capsys):
    assert main(["status", "--json"]) == 3
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["status"] == "error"
    assert doc["exit_code"] == 3
    assert doc["error"]["code"] == 3


def test_clean_without_store_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["clean", "--yes"]) == 0
    assert "nothing to clean" in capsys.readouterr().out


def test_config_json(capsys):
    assert main(["config", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["execution"]["mode"] == "auto"
    assert "token_env" in doc["github"]


def test_unknown_command_treated_as_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # "bogus" is not a command and not a URL -> invalid input exit 3
    assert main(["bogus"]) == 3


def test_json_mode_stdout_is_pure_json(capsys):
    # All prose goes to stderr; stdout parses as one JSON document.
    assert main(["env", "--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert captured.err  # human progress landed on stderr
