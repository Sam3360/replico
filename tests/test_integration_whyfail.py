"""End-to-end WhyFail integration over the real reproduction pipeline.

These run the full flow (workflow parse → venv → execute → verdict → WhyFail
diagnosis) against fixture repositories with a fake GitHub. WhyFail itself is
real and fully offline (it is a declared dependency of Replico).

The fixture failing step runs ``python repro_fail.py`` (a real file, so the
WhyFail CLI can diagnose it — ``python -c`` is not diagnosable by WhyFail 3.x).
"""

from __future__ import annotations

import json

from conftest import (
    FakeGitHub,
    failing_log,
    make_git_repo,
    python_workflow,
    standard_failing_jobs,
)
from replico.config import load_config
from replico.errors import EXIT_FAILURE_EXISTS
from replico.flows import build_app, reproduce, rerun
from replico.github.refs import RunRef
from replico.pipeline import ReproOptions
from replico.security.redaction import Sanitizer
from replico.ui import UI

FAILING_SCRIPT = """\
import os


def load():
    response = {"account": {"id": 7}, "status": "active"}
    return response["user"]


def main():
    token = os.environ.get("DEPLOY_TOKEN", "")
    print(f"deploying with token {token} to production")
    print("FAILED tests/test_auth.py::test_login - AssertionError: boom")
    load()


if __name__ == "__main__":
    main()
"""

# Same logic, but only fails while tests/fixed.flag does not exist — lets a
# rerun "fix" the failure by dropping the file in (acceptance #40).
FLAGGED_SCRIPT = """\
import os
import pathlib


def load():
    response = {"account": {"id": 7}, "status": "active"}
    return response["user"]


def main():
    if pathlib.Path("tests/fixed.flag").exists():
        print("tests passed")
        return
    print("FAILED tests/test_auth.py::test_login - AssertionError: boom")
    load()


if __name__ == "__main__":
    main()
"""


def _context(tmp_path, monkeypatch, *, fake: FakeGitHub, secret: str = ""):
    root, sha = make_git_repo(
        tmp_path,
        files={
            "tests/test_auth.py": "def test_login():\n    pass\n",
            "repro_fail.py": FAILING_SCRIPT,
        },
    )
    monkeypatch.chdir(root)
    if secret:
        monkeypatch.setenv("DEPLOY_TOKEN", secret)
    cfg = load_config()
    ui = UI(plain=True, sanitizer=Sanitizer(), assume_yes=True)
    fake = fake or FakeGitHub(head_sha=sha)
    ctx = build_app(cfg, ui, ui.sanitizer, cwd=root, client=fake)
    return ctx, root


def _fake_for(script: str = "python repro_fail.py") -> FakeGitHub:
    return FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run=script),
        job_log=failing_log(),
    )


def test_reproduce_diagnoses_python_failure_end_to_end(tmp_path, monkeypatch, capsys):
    secret = "deploy-token-7f3k9q2m-local"
    ctx, root = _context(tmp_path, monkeypatch, fake=_fake_for(), secret=secret)

    outcome = reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True),
    )
    assert outcome.verdict.value == "reproduced"
    assert outcome.exit_code == EXIT_FAILURE_EXISTS

    payload = ctx.store.read()
    whyfail = payload.get("whyfail") or {}
    assert whyfail.get("available") is True
    assert whyfail.get("diagnosed") is True
    diagnostics = whyfail.get("diagnostics") or []
    assert diagnostics
    assert diagnostics[0]["exception_type"] == "KeyError"
    cause = (diagnostics[0].get("diagnosis") or {}).get("cause") or {}
    assert cause.get("confidence") == "high"
    # The classification carries the attached diagnosis summary.
    assert (payload.get("classification") or {}).get("whyfail", {}).get(
        "exception_type"
    ) == "KeyError"

    # The human output includes the diagnosis, and never the secret.
    captured = capsys.readouterr()
    assert "WHYFAIL DIAGNOSIS" in captured.out
    assert "KeyError" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err

    # Sanitized artifacts: whyfail.json exists, diagnosis is structured, and no
    # artifact (reproduction.json, whyfail.json, commands.txt, ...) leaks it.
    store_dir = root / ".replico"
    whyfail_file = store_dir / "whyfail.json"
    assert whyfail_file.is_file()
    saved = json.loads(whyfail_file.read_text(encoding="utf-8"))
    assert saved["tool"] == "whyfail"
    assert saved["source"] == "local_reproduction"
    assert saved["diagnostics"][0]["exception_type"] == "KeyError"
    assert saved["diagnostics"][0].get("whyfail") == 1  # real schema preserved
    assert saved["diagnostics"][0].get("schema") == 2
    for file in store_dir.iterdir():
        if file.is_file():
            text = file.read_text(encoding="utf-8")
            assert secret not in text, f"secret leaked into {file.name}"
            assert "ghp_" not in text


def test_reproduce_no_diagnose_flag_skips_cleanly(tmp_path, monkeypatch, capsys):
    ctx, root = _context(tmp_path, monkeypatch, fake=_fake_for())
    outcome = reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True, diagnose=False),
    )
    assert outcome.verdict.value == "reproduced"
    payload = ctx.store.read()
    whyfail = payload.get("whyfail") or {}
    assert whyfail.get("diagnosed") is False
    assert whyfail.get("reason") == "disabled_via_flag"
    assert not (root / ".replico" / "whyfail.json").exists()
    captured = capsys.readouterr()
    assert "WHYFAIL DIAGNOSIS" not in captured.out


def test_reproduce_diagnose_flag_on_unrecognized_command(tmp_path, monkeypatch, capsys):
    # `python -c` cannot be diagnosed by WhyFail — even a forced --diagnose
    # must degrade gracefully instead of failing the reproduction.
    cmd = (
        "echo 'FAILED tests/test_auth.py::test_login - AssertionError: boom'; "
        'python -c "import sys; sys.exit(1)"'
    )
    ctx, _root = _context(tmp_path, monkeypatch, fake=_fake_for(script=cmd))
    outcome = reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True, diagnose=True),
    )
    assert outcome.verdict.value == "reproduced"
    whyfail = ctx.store.read().get("whyfail") or {}
    assert whyfail.get("diagnosed") is False
    assert whyfail.get("reason") == "unsupported_failure_type"
    captured = capsys.readouterr()
    assert "WhyFail diagnosis skipped" in captured.out


def test_rerun_still_failing_diagnoses_again_and_status_shows_it(tmp_path, monkeypatch, capsys):
    ctx, _root = _context(tmp_path, monkeypatch, fake=_fake_for())
    reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True),
    )
    second = rerun(ctx, ReproOptions(assume_yes=True))
    assert second.verdict.value == "reproduced"
    payload = ctx.store.read()
    assert payload["whyfail"]["diagnosed"] is True
    reruns = payload.get("reruns") or []
    assert len(reruns) == 1
    diagnosis = reruns[-1].get("diagnosis") or {}
    assert diagnosis.get("diagnosed") is True
    assert (diagnosis.get("summary") or {}).get("exception_type") == "KeyError"
    capsys.readouterr()  # discard rerun output


def test_rerun_fix_reports_not_reproduced_without_false_claim(tmp_path, monkeypatch, capsys):
    fake = FakeGitHub(
        jobs=standard_failing_jobs(),
        workflow_yaml=python_workflow(extra_run="python repro_fail.py"),
        job_log=failing_log(),
    )
    ctx, root = _context(tmp_path, monkeypatch, fake=fake)
    # Swap in the flag-controlled script (simulating the developer's fix).
    (root / "repro_fail.py").write_text(FLAGGED_SCRIPT, encoding="utf-8")
    reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True),
    )
    assert ctx.store.read()["whyfail"]["diagnosed"] is True

    # Developer "fixes" the code: add the flag file.
    (root / "tests" / "fixed.flag").write_text("fixed", encoding="utf-8")
    second = rerun(ctx, ReproOptions(assume_yes=True))
    assert second.verdict.value == "not_reproduced"
    assert second.exit_code == 0
    payload = ctx.store.read()
    assert payload["whyfail"]["diagnosed"] is False
    assert payload["whyfail"]["reason"] == "no_local_failure"
    # No stale diagnosis artifact survives a passing rerun.
    assert not (root / ".replico" / "whyfail.json").exists()
    captured = capsys.readouterr()
    assert "NOT REPRODUCED" in captured.out


def test_diagnose_command_offline(tmp_path, monkeypatch, capsys):
    ctx, _root = _context(tmp_path, monkeypatch, fake=_fake_for())
    reproduce(
        ctx,
        RunRef(owner="octocat", repo="demo", run_id=123456789),
        ReproOptions(assume_yes=True),
    )
    capsys.readouterr()

    # The command goes through the real CLI (still fully offline).
    from replico.cli import main

    code = main(["diagnose", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["command"] == "diagnose"
    whyfail = doc["whyfail"]
    assert whyfail["diagnosed"] is True
    assert whyfail["diagnostics"][0]["exception_type"] == "KeyError"


def test_diagnose_without_store_is_invalid_input(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from replico.cli import main

    assert main(["diagnose"]) == 3
    assert "no saved reproduction" in capsys.readouterr().out
