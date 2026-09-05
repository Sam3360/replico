"""Storage tests: .replico/ round trips, redaction, format validation."""

from __future__ import annotations

import json

import pytest

from replico.errors import InvalidInputError
from replico.security.redaction import Sanitizer
from replico.storage.store import ReproductionStore


def _payload() -> dict:
    return {
        "tool_version": "test",
        "run": {"run_id": 1, "owner": "o", "repo": "r", "head_sha": "a" * 40},
        "failure": {"tests": ["t::x"], "summary": "AssertionError"},
        "workflow_yaml": "name: CI\n",
        "environment": {"os": "Windows"},
        "differences": [{"ok": False, "label": "OS", "detail": "diff"}],
        "parity": 50,
        "execution": {"mode": "local_venv", "install_commands": [], "target_commands": []},
        "verdict": {"verdict": "reproduced", "exit_code": 1},
        "setup_commands": [
            {"comment": "install", "text": "pip install -r requirements.txt", "shell": "bash"}
        ],
        "target_commands": [{"comment": "Run tests", "text": "pytest -q", "shell": "bash"}],
        "reruns": [],
    }


def test_roundtrip(tmp_path):
    store = ReproductionStore(tmp_path)
    store.write(_payload())
    data = store.read()
    assert data["format_version"] == 1
    assert data["run"]["run_id"] == 1
    assert data["verdict"]["verdict"] == "reproduced"


def test_expected_files_created(tmp_path):
    store = ReproductionStore(tmp_path)
    store.write(_payload())
    names = sorted(p.name for p in store.dir.iterdir())
    for expected in (
        "environment.json",
        "reproduction.json",
        "workflow.yml",
        "commands.txt",
        "differences.json",
        "README.md",
    ):
        assert expected in names


def test_write_redacts_every_file(tmp_path):
    sanitizer = Sanitizer(extra_secrets=["literal-secret-value-1"])
    secret_log = "deploying with literal-secret-value-1 now"
    payload = _payload()
    payload["failure"]["summary"] = secret_log
    payload["target_commands"][0]["text"] = f"echo {secret_log}"
    payload["workflow_yaml"] = f"# {secret_log}\nname: CI\n"
    payload["verdict"]["reasons"] = [secret_log]
    store = ReproductionStore(tmp_path, sanitizer=sanitizer)
    store.write(payload)
    for file in store.dir.iterdir():
        if file.is_file() and file.name != "README.md":
            assert "literal-secret-value-1" not in file.read_text(encoding="utf-8")
    # GitHub-style tokens are caught by SecretShield patterns too:
    payload2 = _payload()
    payload2["failure"]["evidence_lines"] = ["token=ghp_" + "B" * 36]
    store2 = ReproductionStore(tmp_path, sanitizer=Sanitizer())
    store2.write(payload2)
    text = (store2.dir / "reproduction.json").read_text(encoding="utf-8")
    assert "ghp_" + "B" * 36 not in text


def test_commands_txt_content(tmp_path):
    store = ReproductionStore(tmp_path)
    store.write(_payload())
    text = (store.dir / "commands.txt").read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in text
    assert "pytest -q" in text


def test_clean_removes_everything(tmp_path):
    store = ReproductionStore(tmp_path)
    store.write(_payload())
    assert store.dir.exists()
    store.clean()
    assert not store.dir.exists()
    assert not store.exists()


def test_read_corrupt_raises(tmp_path):
    store = ReproductionStore(tmp_path)
    store.dir.mkdir(parents=True)
    (store.dir / "reproduction.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidInputError):
        store.read()


def test_read_missing_raises(tmp_path):
    with pytest.raises(InvalidInputError):
        ReproductionStore(tmp_path).read()


def test_wrong_format_version_rejected(tmp_path):
    store = ReproductionStore(tmp_path)
    store.dir.mkdir(parents=True)
    payload = _payload()
    payload["format_version"] = 999
    (store.dir / "reproduction.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InvalidInputError):
        store.read()
