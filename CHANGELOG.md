# Changelog

All notable changes to Replico are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and semantic
versioning (see [pyproject.toml](pyproject.toml)).

## [0.2.0] - 2026-09-04

WhyFail integration: reproduced Python failures are now diagnosed locally
with evidence-based root-cause analysis, on top of the untouched v0.1
reproduction engine.

### Added

- **WhyFail 3.x integration** — when a reproduced local run fails and the
  failing step is a python/pytest invocation, Replico runs WhyFail's
  structured CLI (`python -m whyfail.cli run --format json …`) against the
  same command in the reproduction environment and consumes the real
  `Diagnostic.to_dict()` schema (never scraped terminal text). See
  `replico/analysis/whyfail_adapter.py`.
- **`replico diagnose`** — re-runs the saved failing command under WhyFail
  and renders the structured diagnosis. Fully offline; `--json` supported.
- **`--diagnose` / `--no-diagnose`** flags on `reproduce`/`run`/`rerun`
  (auto = diagnose eligible reproduced Python failures).
- **Automatic diagnosis** after a reproduced Python failure in
  `replico <run-url>` and `replico rerun`, with a combined
  reproduction + diagnosis report.
- **`.replico/whyfail.json`** — sanitized structured diagnosis artifact;
  the same block is embedded in `reproduction.json` and `--json` output as
  `whyfail: {available, diagnosed, reason, diagnostics}`.
- **`[diagnostics]` configuration** — `enabled` and `whyfail` switches
  (`.replico.toml`). Diagnosis can be switched off; secret redaction stays
  mandatory.
- **Status/diff diagnosis awareness** — `replico status` reports whether a
  diagnosis is available and its confidence; `replico diff` compares the
  original diagnosis with the latest rerun's when both exist.
- **Failure classification additions** — `PYTHON_EXCEPTION`,
  `AUTHENTICATION_REQUIRED`, `UNSUPPORTED_ACTION`, `WORKFLOW_PARSE_ERROR`,
  `COMMAND_FAILURE`, `INFRASTRUCTURE_FAILURE`; WhyFail summaries attach to
  `classification.whyfail` in saved payloads.

### Improved

- Environment parity and Docker reasoning: when the CI runner OS differs
  from the local OS and Docker is unavailable, the plan says so explicitly
  instead of silently proceeding with reduced parity.
- Secret hygiene for diagnostics: WhyFail redacts runtime values with its
  own SecretShield layer, and every diagnostic is re-sanitized by Replico's
  `Sanitizer` before display or persistence.

### Preserved

- v0.1 CLI compatibility — all existing commands, flags, exit codes and
  JSON documents keep working (the `whyfail` key is additive).
- SecretShield stays a first-class Replico dependency for CI logs,
  environment data and artifacts (Replico does not rely on WhyFail's
  redaction alone).
- Local-first, offline, deterministic, no telemetry, no cloud dependency.

## [0.1.0] - 2026-09-04

First public release: GitHub Actions failures → local reproduction for
Python workflows.

### Added

- **Core command** — `replico <run-url>` (and `replico reproduce …`,
  `replico run <run-id>`) walks a failed GitHub Actions run end to end:
  run identification → failed job → failed step → workflow parsing →
  environment/dependency detection → reproduction plan → safe local
  execution → honest verdict.
- **GitHub integration** — URL/ref parsing for `actions/runs/<id>` links,
  `GITHUB_TOKEN` and `gh` CLI auth, public-repo anonymous access, run/job/
  step listing, log download, workflow YAML retrieval by commit SHA.
- **Workflow parser** — hardened PyYAML-based parser (memoized alias
  expansion, node budgets, depth caps against billion-laughs/recursion
  attacks) supporting `runs-on`, `steps`, `uses`, `run`, `with`, `env`,
  `defaults`, `working-directory`, `shell`, `strategy.matrix`, `if`.
- **Job/step selection** — automatic failed-job detection with interactive
  picker when several fail; `--job` to select explicitly; failed-step
  identification from job conclusion + annotations.
- **Ecosystem adapters** — pluggable `EcosystemAdapter` protocol; v0.1
  ships Python (setup-action versions, requirements/pyproject/setup files,
  `pip install`/`pytest`/`unittest` detection). Node adapter registers but
  reports unsupported until v0.3.
- **Local execution** — explicit-interpreter script runner (no `shell=True`),
  per-shell availability probing, risk audit of every workflow command
  (elevation/destructive/network-exec/suspicious patterns) with mandatory
  confirmation, optional `--docker` isolation with automatic fallback.
- **Failure analysis** — log compression to relevant evidence lines,
  evidence-based classification (test/build/dependency/version/missing
  tool/file/env/timeout/network/permission/config/unknown), confidence
  scoring, GitHub annotation anchors.
- **Verdicts** — `reproduced`, `partially_reproduced`, `not_reproduced`,
  `unsupported`; environment parity estimate comparing local machine to the
  CI runner; never over-claims reproduction.
- **SecretShield integration** — `Sanitizer` adapter over the public
  SecretShield API (`redact`/`detect`/`configure`) plus a compatibility
  layer for known low-entropy literal secrets; every UI line, JSON payload
  and `.replico/` artifact passes through it. Debug/verbose modes respect
  redaction; stream-level protection via `enable()` where appropriate.
- **`.replico/` store** — redacted `reproduction.json`, `environment.json`,
  `workflow.yml`, `commands.txt`, `differences.json`, `README.md`;
  `rerun`, `status`, `diff`, `env`, `clean`, `capture` commands.
- **CLI polish** — rich formatting with `--plain`, machine-readable `--json`,
  stable exit codes (0–6, 70), progress lines, sensible colors.
- **Offline test suite** — mocked GitHub API (`FakeGitHub`), fixture
  repositories created on the fly, security tests for leakage, injection,
  traversal, malicious YAML/filenames/env.
- **Packaging/docs** — modern `pyproject.toml` (hatchling), console entry
  point, README, SECURITY model, own GitHub Actions CI
  (3 OS × Python 3.11–3.14 + lint/types/build validation).

### Security

- New dependency: `secretshield>=0.4.2` (see `replico/security/redaction.py`).
- Workflow commands are audited before execution; nothing elevated or
  destructive runs without explicit `--yes`-free confirmation.
- Repository/job names are validated before touching URLs, paths or
  subprocess arguments; `safe_join` blocks path traversal.

### Notes / limitations

- v0.1 targets Python workflows on public repositories. Node/Go/Rust and
  Docker-first flows are recognized but reported as unsupported rather than
  half-reproduced.
- The environment parity figure is a transparent heuristic over a handful of
  weighted checks — an estimate, never a guarantee.
