from conftest import make_git_repo
from replico.gitrepo import (
    _parse_remote_url,
    find_git_repo,
    matches_run_repo,
)


def test_parse_https_remote():
    remote = _parse_remote_url("https://github.com/octocat/demo.git")
    assert (remote.owner, remote.repo, remote.host) == ("octocat", "demo", "github.com")


def test_parse_ssh_remote():
    remote = _parse_remote_url("git@github.com:octocat/demo.git")
    assert (remote.owner, remote.repo) == ("octocat", "demo")


def test_parse_remote_with_credentials_strips_them():
    remote = _parse_remote_url("https://user:secret@github.com/o/r.git")
    assert (remote.owner, remote.repo) == ("o", "r")
    assert "secret" not in remote.url


def test_parse_garbage_remote():
    assert _parse_remote_url("") is None
    assert _parse_remote_url("just-a-word") is None


def test_find_repo_and_state(tmp_path):
    root, sha = make_git_repo(tmp_path)
    repo = find_git_repo(root)
    assert repo is not None
    assert repo.head_sha == sha
    assert repo.remote is not None
    assert repo.remote.repo == "demo"
    assert matches_run_repo(repo, "octocat", "demo")
    assert not matches_run_repo(repo, "someone", "other")


def test_repo_from_subdirectory(tmp_path):
    root, _sha = make_git_repo(tmp_path, files={"pkg/__init__.py": ""})
    sub = root / "pkg"
    repo = find_git_repo(sub)
    assert repo is not None and repo.root == root


def test_not_a_repo(tmp_path):
    assert find_git_repo(tmp_path) is None


def test_malicious_remote_repo_rejected(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    repo = find_git_repo(root)  # not a repo -> None before any parsing
    assert repo is None
