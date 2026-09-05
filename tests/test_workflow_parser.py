"""Workflow parsing tests, including malicious-YAML handling."""

from __future__ import annotations

import pytest

from replico.errors import InvalidInputError, UnsupportedError
from replico.workflow.parser import materialize_yaml, parse_workflow

SIMPLE = """name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
  test:
    runs-on: ubuntu-24.04
    needs: lint
    strategy:
      matrix:
        python: ['3.11', '3.12']
    env:
      CI_MODE: nightly
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install
        run: |
          pip install -r requirements.txt
          pip install -e .
        working-directory: src
      - name: Run tests
        run: python -m pytest tests/
        env:
          TOKEN: ${{ secrets.CI_TOKEN }}
"""


def test_parse_basic_workflow():
    workflow = parse_workflow(SIMPLE)
    assert workflow.display_name == "CI"
    assert set(workflow.jobs) == {"lint", "test"}
    job = workflow.jobs["test"]
    assert job.runs_on == ["ubuntu-24.04"]
    assert job.needs == ["lint"]
    assert job.env == {"CI_MODE": "nightly"}
    steps = job.steps
    assert [s.uses for s in steps] == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
        None,
        None,
    ]
    install = steps[2]
    assert "pip install -r requirements.txt" in install.run
    assert install.working_directory == "src"
    assert job.matrix is not None
    assert job.matrix.axes == [("python", ["3.11", "3.12"])]


def test_env_secret_names_referenced():
    workflow = parse_workflow(SIMPLE)
    job = workflow.jobs["test"]
    step_env = job.steps[3].env
    assert step_env == {"TOKEN": "${{ secrets.CI_TOKEN }}"}
    assert "secrets.CI_TOKEN" in step_env["TOKEN"]


def test_duplicate_merge_and_anchors():
    text = """
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    env: &base
      A: '1'
    steps:
      - run: echo ok
"""
    workflow = parse_workflow(text)
    assert workflow.jobs["build"].env == {"A": "1"}


def test_merge_key_supported():
    text = """
jobs:
  a:
    env:
      shared: &shared
        X: '1'
      extra: '2'
  b:
    env:
      <<: *shared
      Y: '3'
"""
    workflow = parse_workflow(text)
    assert workflow.jobs["b"].env == {"X": "1", "Y": "3"}


def test_missing_jobs_rejected():
    with pytest.raises(UnsupportedError):
        parse_workflow("name: nothing\non: [push]\n")


def test_top_level_not_mapping_rejected():
    with pytest.raises(InvalidInputError):
        parse_workflow("- just\n- a\n- list\n")


def test_invalid_yaml_rejected():
    with pytest.raises(InvalidInputError):
        parse_workflow("jobs: [unclosed\n  steps: {bad")


def test_billion_laughs_bomb_is_neutralized():
    bomb = 'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
    for i in range(9):
        prev = chr(ord("a") + i)
        nxt = chr(ord("a") + i + 1)
        bomb += f"{nxt}: &{nxt} [*{prev},*{prev},*{prev},*{prev},*{prev},*{prev},*{prev},*{prev},*{prev}]\n"
    data = materialize_yaml(bomb)
    assert isinstance(data, dict)
    assert len(data["j"]) == 9  # only the unique levels materialize


def test_deep_nesting_rejected():
    deep = "x: " + "[" * 2000 + "1" + "]" * 2000
    with pytest.raises(InvalidInputError):
        materialize_yaml(deep)


def test_empty_input_rejected():
    with pytest.raises(InvalidInputError):
        parse_workflow("")
