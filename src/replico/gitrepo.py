"""Local git integration: find the repo, learn its state, diff it.

All git invocations go through a subprocess without a shell. Replico never
commits, pushes or otherwise mutates the repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from replico.errors import SetupError
from replico.util import UnsafeNameError, decode_bytes, validate_repo_name

_GIT_TIMEOUT = 30


@dataclass
class GitRemote:
    host: str
    owner: str
    repo: str
    url: str


@dataclass
class GitRepo:
    root: Path
    head_sha: str = ""
    head_branch: str | None = None
    remote: GitRemote | None = None
    dirty_files: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str | None:
        return f"{self.remote.owner}/{self.remote.repo}" if self.remote else None

    def run_git(self, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd or self.root,
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SetupError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SetupError("git command timed out") from exc

    def changed_since(self, sha: str) -> list[str]:
        """Files differing between `sha` and the current working tree."""
        result = self.run_git(["diff", "--name-only", sha, "HEAD", "--"])
        if result.returncode != 0:
            return []
        return [line for line in decode_bytes(result.stdout).splitlines() if line.strip()]

    def has_commit(self, sha: str) -> bool:
        result = self.run_git(["cat-file", "-e", f"{sha}^{{commit}}"])
        return result.returncode == 0

    def diff_stat_since(self, sha: str) -> str:
        result = self.run_git(["diff", "--stat", sha, "HEAD", "--"])
        return decode_bytes(result.stdout).strip()


def _parse_remote_url(url: str) -> GitRemote | None:
    """Parse an https/ssh/git remote URL into (host, owner, repo)."""
    url = url.strip()
    if not url:
        return None
    if url.startswith("git@") and ":" in url:
        body = url[len("git@") :]
        host, _, path = body.partition(":")
        path = path.removesuffix(".git")
    elif url.startswith(("https://", "http://", "git://")):
        scheme_end = url.index("://") + 3
        rest = url[scheme_end:]
        # Strip any credentials: https://user:pass@host/...
        rest = rest.split("@", 1)[-1]
        host, _, path = rest.partition("/")
        path = path.removesuffix(".git").strip("/")
    else:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    if len(parts) >= 2:
        owner, repo = parts[0], parts[1]
    else:
        owner, repo = "", parts[0]
    # Never retain credentials in the stored URL.
    clean_url = url
    if "://" in clean_url and "@" in clean_url.split("://", 1)[1]:
        scheme, _, remainder = clean_url.partition("://")
        clean_url = f"{scheme}://{remainder.split('@', 1)[-1]}"
    return GitRemote(host=host.lower(), owner=owner, repo=repo, url=clean_url)


def find_git_repo(start: Path | None = None) -> GitRepo | None:
    """Locate the repository containing `start` (default cwd), if any."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            try:
                return _build_repo(directory)
            except (SetupError, UnsafeNameError):
                return None
    return None


def _build_repo(root: Path) -> GitRepo:
    repo = GitRepo(root=root)
    sha_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if sha_result.returncode == 0:
        repo.head_sha = decode_bytes(sha_result.stdout).strip()

    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    branch = decode_bytes(branch_result.stdout).strip()
    repo.head_branch = branch if branch and branch != "HEAD" else None

    remote_result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if remote_result.returncode == 0:
        parsed = _parse_remote_url(decode_bytes(remote_result.stdout))
        if parsed:
            if parsed.owner:
                validate_repo_name(parsed.owner)
            if parsed.repo:
                validate_repo_name(parsed.repo)
            repo.remote = parsed

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if status_result.returncode == 0:
        repo.dirty_files = [
            line[3:].strip() if len(line) > 3 else line
            for line in decode_bytes(status_result.stdout).splitlines()
        ]
    return repo


def matches_run_repo(repo: GitRepo, owner: str, repo_name: str) -> bool:
    """Does the local origin correspond to the workflow run's repository?

    A local checkout of a fork (or of the upstream) both share the repo
    name, so we compare the repo name and only require owner equality when
    the remote is unambiguous.
    """
    if repo.remote is None:
        return False
    return repo.remote.repo.lower() == repo_name.lower() and repo.remote.host in (
        "github.com",
        "",
    )
