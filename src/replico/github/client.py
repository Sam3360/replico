"""GitHub API client.

Local-first: requests are only made to api.github.com (or the configured
``api_base``) and only when GitHub information is actually required. Tokens
are resolved lazily, travel only in the Authorization header, and are never
logged or included in exception text.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from typing import Any

import requests

from replico.errors import AuthError, SetupError
from replico.github.refs import RunRef
from replico.models import JobInfo, RunInfo, StepRunInfo
from replico.util import decode_bytes

log = logging.getLogger("replico.github")

API_VERSION = "2022-11-28"
_UA = "replico/0.1"


class ApiError(Exception):
    def __init__(self, status: int, reason: str, *, payload: Any = None) -> None:
        super().__init__(f"GitHub API error {status}: {reason}")
        self.status = status
        self.reason = reason
        self.payload = payload


class NotFoundError(ApiError):
    pass


def _gh_auth_token() -> str | None:
    """Token from the GitHub CLI (``gh auth token``), if available."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    token = decode_bytes(result.stdout).strip()
    return token or None


class GitHubClient:
    """Thin, testable wrapper around the GitHub REST API."""

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base: str = "https://api.github.com",
        timeout_s: int = 60,
        log_max_bytes: int = 6 * 1024 * 1024,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s
        self.log_max_bytes = log_max_bytes
        self._session = session or requests.Session()
        self._resolved_gh_token: str | None = None

    # -- token handling -----------------------------------------------------

    def _effective_token(self) -> str | None:
        if self._token:
            return self._token
        env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if env_token:
            return env_token
        if self._resolved_gh_token is None:
            # Probe once; cache the outcome (including "no gh available").
            self._resolved_gh_token = _gh_auth_token()
        return self._resolved_gh_token

    # -- transport ----------------------------------------------------------

    def _headers(self, *, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": _UA,
            "X-GitHub-Api-Version": API_VERSION,
        }
        token = self._effective_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        accept: str = "application/vnd.github+json",
        stream: bool = False,
        allow_retry: bool = True,
    ) -> requests.Response:
        url = f"{self.api_base}{path}"
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                headers=self._headers(accept=accept),
                timeout=self.timeout_s,
                stream=stream,
            )
        except requests.exceptions.RequestException as exc:
            raise SetupError(f"cannot reach GitHub API ({exc.__class__.__name__})") from exc

        if response.status_code in (401, 403) and self._effective_token() is None and allow_retry:
            log.debug("GitHub API %s without token; retrying with gh token", response.status_code)
            # Force resolution attempt via gh before giving up.
            gh_token = _gh_auth_token()
            if gh_token:
                self._resolved_gh_token = gh_token
                return self.request(
                    method, path, params=params, accept=accept, stream=stream, allow_retry=False
                )
        if response.status_code in (401, 403):
            if "rate limit" in (response.text or "").lower():
                raise AuthError(
                    "GitHub API rate limit reached — set GITHUB_TOKEN for a higher limit",
                    hint="export GITHUB_TOKEN=ghp_...",
                )
            raise AuthError(
                "GitHub requires authentication for this request "
                "(private repository, or rate limit). "
                "Set GITHUB_TOKEN or authenticate the GitHub CLI (`gh auth login`).",
                hint="export GITHUB_TOKEN=ghp_...  # or: gh auth login",
            )
        if response.status_code == 404:
            raise NotFoundError(
                404, "not found (repository may be private or the run may not exist)"
            )
        if response.status_code == 410:
            raise NotFoundError(410, "log file no longer available (expired)")
        if response.status_code >= 400:
            raise ApiError(
                response.status_code,
                response.reason or "error",
                payload=response.text[:500],
            )
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(200, f"invalid JSON from {path}") from exc

    # -- domain endpoints ---------------------------------------------------

    def get_run(self, ref: RunRef) -> RunInfo:
        data = self._json("GET", f"/repos/{ref.owner}/{ref.repo}/actions/runs/{ref.run_id}")
        return RunInfo(
            id=int(data["id"]),
            owner=ref.owner,
            repo=ref.repo,
            workflow_id=int(data.get("workflow_id") or 0),
            workflow_name=str(data.get("name") or data.get("display_title") or ""),
            head_sha=str(data.get("head_sha") or ""),
            head_branch=data.get("head_branch"),
            event=str(data.get("event") or ""),
            status=str(data.get("status") or ""),
            conclusion=data.get("conclusion"),
            html_url=data.get("html_url"),
            display_title=data.get("display_title"),
        )

    def get_jobs(self, ref: RunRef, run_id: int | None = None) -> list[JobInfo]:
        data = self._json(
            "GET",
            f"/repos/{ref.owner}/{ref.repo}/actions/runs/{run_id or ref.run_id}/jobs",
            params={"per_page": 100},
        )
        jobs: list[JobInfo] = []
        for item in data.get("jobs", []):
            steps = [
                StepRunInfo(
                    number=int(step.get("number") or 0),
                    name=str(step.get("name") or ""),
                    conclusion=step.get("conclusion"),
                    status=step.get("status"),
                )
                for step in item.get("steps", [])
            ]
            jobs.append(
                JobInfo(
                    id=int(item["id"]),
                    name=str(item.get("name") or ""),
                    conclusion=item.get("conclusion"),
                    status=item.get("status"),
                    steps=steps,
                    labels=list(item.get("labels") or []),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                )
            )
        return jobs

    def get_workflow_path(self, ref: RunRef, workflow_id: int) -> str:
        data = self._json("GET", f"/repos/{ref.owner}/{ref.repo}/actions/workflows/{workflow_id}")
        return str(data.get("path") or "")

    def get_file_content(self, ref: RunRef, path: str, ref_sha: str) -> str:
        data = self._json(
            "GET",
            f"/repos/{ref.owner}/{ref.repo}/contents/{path}",
            params={"ref": ref_sha},
        )
        content = data.get("content")
        if content is None:
            raise NotFoundError(404, f"no content for {path}")
        try:
            return decode_bytes(base64.b64decode(content))
        except (ValueError, TypeError) as exc:
            raise ApiError(200, f"could not decode {path}") from exc

    def get_job_logs(self, ref: RunRef, job_id: int) -> str:
        """Raw log text for a job, capped to ``log_max_bytes``."""
        response = self.request(
            "GET",
            f"/repos/{ref.owner}/{ref.repo}/actions/jobs/{job_id}/logs",
            accept="application/vnd.github+json",
            stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.log_max_bytes:
                    chunks.append(b"\n[truncated by replico: log exceeds configured limit]\n")
                    break
                chunks.append(chunk)
        finally:
            response.close()
        return decode_bytes(b"".join(chunks))
