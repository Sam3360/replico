"""GitHub client tests with a mocked HTTP transport (fully offline)."""

from __future__ import annotations

import base64

import pytest

from replico.errors import AuthError
from replico.github.client import (
    ApiError,
    GitHubClient,
    NotFoundError,
    _gh_auth_token,
)
from replico.github.refs import RunRef

REF = RunRef(owner="octocat", repo="demo", run_id=42)


class _Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.reason = "ok" if status < 400 else "error"
        self._iter = None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024):
        if self._iter is None:
            self._iter = [self.text.encode()]
        return iter(self._iter)

    def close(self):
        pass


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers", {}),
                "params": kwargs.get("params"),
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        return self.responses.pop(0)


def _client(responses, *, token: str | None = None):
    return GitHubClient(token=token, session=_FakeSession(responses))


def test_get_run_parses_fields():
    client = _client(
        [
            _Response(
                payload={
                    "id": 42,
                    "workflow_id": 7,
                    "name": "CI",
                    "head_sha": "a" * 40,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                }
            )
        ]
    )
    run = client.get_run(REF)
    assert run.id == 42
    assert run.workflow_id == 7
    assert run.head_sha == "a" * 40
    assert run.conclusion == "failure"


def test_get_jobs_parses_steps():
    client = _client(
        [
            _Response(
                payload={
                    "jobs": [
                        {
                            "id": 1,
                            "name": "test",
                            "conclusion": "failure",
                            "status": "completed",
                            "steps": [
                                {"number": 1, "name": "Checkout", "conclusion": "success"},
                                {"number": 2, "name": "Run tests", "conclusion": "failure"},
                            ],
                        }
                    ]
                }
            )
        ]
    )
    jobs = client.get_jobs(REF)
    assert jobs[0].name == "test"
    assert jobs[0].steps[1].name == "Run tests"
    assert jobs[0].steps[1].conclusion == "failure"


def test_get_workflow_and_file_content():
    content = base64.b64encode(b"name: CI\njobs: {}\n").decode()
    client = _client(
        [
            _Response(payload={"path": ".github/workflows/ci.yml"}),
            _Response(payload={"content": content}),
        ]
    )
    path = client.get_workflow_path(REF, 7)
    assert path == ".github/workflows/ci.yml"
    text = client.get_file_content(REF, path, "a" * 40)
    assert text.startswith("name: CI")


def test_404_maps_to_not_found():
    client = _client([_Response(status=404)])
    with pytest.raises(NotFoundError):
        client.get_run(REF)


def test_403_without_token_is_auth_error():
    client = _client([_Response(status=403, text="rate limit exceeded")])
    with pytest.raises(AuthError):
        client.get_run(REF)


def test_401_without_token_is_auth_error():
    client = _client([_Response(status=401, text="Bad credentials")])
    with pytest.raises(AuthError):
        client.get_run(REF)


def test_token_from_environment_used(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xyz")
    client = _client([_Response(payload={"id": 42, "head_sha": "x"})])
    client.get_run(REF)
    headers = client._session.requests[0]["headers"]
    assert headers.get("Authorization") == "Bearer ghp_xyz"


def test_gh_cli_token_used_when_env_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("replico.github.client._gh_auth_token", lambda: "gho_fallback")
    client = _client([_Response(payload={"id": 42, "head_sha": "x"})])
    client.get_run(REF)
    assert client._session.requests[0]["headers"]["Authorization"] == "Bearer gho_fallback"


def test_token_never_in_error_text():
    client = _client([_Response(status=500, text="boom")])
    with pytest.raises(ApiError) as exc:
        client.get_run(REF)
    assert "Bearer" not in str(exc.value)
    assert "ghp_" not in str(exc.value)


def test_job_logs_streamed():
    client = _client([_Response(status=200, text="line1\nline2\n")])
    logs = client.get_job_logs(REF, 9)
    assert "line1" in logs
    assert "repos/octocat/demo/actions/jobs/9/logs" in client._session.requests[0]["url"]


def test_job_logs_truncated(monkeypatch):
    client = _client([_Response(status=200, text="y" * 10000)])
    client.log_max_bytes = 100
    logs = client.get_job_logs(REF, 9)
    assert "[truncated" in logs


def test_gh_auth_token_command(monkeypatch):
    import subprocess

    class _P:
        returncode = 0
        stdout = b"gho_from_cli\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    assert _gh_auth_token() == "gho_from_cli"

    class _Fail:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fail())
    assert _gh_auth_token() is None


def test_network_error_maps_to_setup_error():
    import requests

    class _Boom:
        def request(self, *a, **k):
            raise requests.exceptions.ConnectionError("dns failed")

    client = GitHubClient(session=_Boom())
    from replico.errors import SetupError

    with pytest.raises(SetupError):
        client.get_run(REF)
