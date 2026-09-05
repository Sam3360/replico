"""GitHub run reference parsing (URLs and run ids)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from replico.errors import InvalidInputError
from replico.util import UnsafeNameError, validate_repo_name

_RUN_URL_RE = re.compile(
    r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)"
)
_RUN_ID_RE = re.compile(r"^\d{1,12}$")


@dataclass(frozen=True)
class RunRef:
    owner: str
    repo: str
    run_id: int
    host: str = "github.com"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def html_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/actions/runs/{self.run_id}"


def parse_run_url(url: str) -> RunRef:
    """Validate and split a GitHub Actions run URL.

    Accepts URLs of the shape
    ``https://github.com/<owner>/<repo>/actions/runs/<run_id>`` with an
    optional trailing path (e.g. ``/job/1234``), query string or fragment.
    """
    text = url.strip()
    if not text:
        raise InvalidInputError("empty run URL")
    if "://" not in text:
        raise InvalidInputError(
            f"not a URL: {text!r} (expected https://github.com/<owner>/<repo>/actions/runs/<id>)"
        )
    match = _RUN_URL_RE.match(text)
    if not match:
        raise InvalidInputError(
            "unsupported URL format — expected "
            "https://github.com/<owner>/<repo>/actions/runs/<run-id>"
        )
    host = match.group("host").lower()
    if host != "github.com":
        raise InvalidInputError(
            f"unsupported GitHub host {host!r} (only github.com is supported in v0.1)"
        )
    owner, repo = match.group("owner"), match.group("repo")
    run_id = int(match.group("run_id"))
    try:
        validate_repo_name(owner)
        validate_repo_name(repo)
    except UnsafeNameError as exc:
        raise InvalidInputError(str(exc)) from exc
    return RunRef(owner=owner, repo=repo, run_id=run_id, host=host)


def looks_like_run_id(token: str) -> bool:
    return bool(_RUN_ID_RE.match(token.strip()))


def parse_run_id(token: str) -> int:
    token = token.strip()
    if not _RUN_ID_RE.match(token):
        raise InvalidInputError(f"invalid run id {token!r} (expected a number)")
    return int(token)
