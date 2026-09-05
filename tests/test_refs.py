import pytest

from replico.errors import InvalidInputError
from replico.github.refs import parse_run_id, parse_run_url


def test_valid_run_url():
    ref = parse_run_url("https://github.com/octocat/hello-world/actions/runs/123456789")
    assert (ref.owner, ref.repo, ref.run_id) == ("octocat", "hello-world", 123456789)


def test_run_url_with_query_fragment_and_job_path():
    ref = parse_run_url("https://github.com/octocat/demo/actions/runs/42/job/999?pr=1#summary")
    assert (ref.owner, ref.repo, ref.run_id) == ("octocat", "demo", 42)


def test_run_url_trailing_slash():
    ref = parse_run_url("https://github.com/o/r/actions/runs/7/")
    assert ref.run_id == 7


def test_invalid_host_rejected():
    with pytest.raises(InvalidInputError):
        parse_run_url("https://gitlab.com/o/r/actions/runs/1")


def test_wrong_path_rejected():
    with pytest.raises(InvalidInputError):
        parse_run_url("https://github.com/o/r/commits/abc")
    with pytest.raises(InvalidInputError):
        parse_run_url("https://github.com/o/r/actions/runs/notanumber")


def test_not_a_url_rejected():
    with pytest.raises(InvalidInputError):
        parse_run_url("hello world")


def test_malicious_owner_repo_rejected():
    # Path traversal / shell metacharacters must never become URLs or paths.
    for name in ("..", "a/b", "a\\b", "rm -rf", "$(id)", "a;b"):
        with pytest.raises(InvalidInputError):
            parse_run_url(f"https://github.com/{name}/repo/actions/runs/1")
        with pytest.raises(InvalidInputError):
            parse_run_url(f"https://github.com/owner/{name}/actions/runs/1")


def test_run_id_parsing():
    assert parse_run_id("123") == 123
    with pytest.raises(InvalidInputError):
        parse_run_id("12x")
    with pytest.raises(InvalidInputError):
        parse_run_id("")


def test_html_url_roundtrip():
    ref = parse_run_url("https://github.com/a/b/actions/runs/9")
    assert ref.html_url == "https://github.com/a/b/actions/runs/9"
