# Replico

**CI failures, reproduced locally.**

GitHub Actions fails → `replico <run-url>` → the failing command runs on your
machine → you get a **reproducible failure** with evidence — and, for Python
failures, an evidence-based explanation of *why* it happened.

Replico is a local-first developer tool. It reads a failed GitHub Actions run,
figures out *which job failed, which step failed and why*, reconstructs the
relevant environment (Python version, dependencies, environment variables it
can), and replays the failing step on your machine. It then tells you — with
honest confidence levels — whether the CI failure was reproduced. When the
reproduced failure is a Python exception, **WhyFail** diagnoses it from the
local runtime evidence, and **SecretShield** keeps every piece of evidence
redacted.

> **The core promise:** turn CI failures into locally reproducible failures
> whenever Replico can reconstruct the relevant conditions. Replico never
> claims to be a perfect clone of a GitHub-hosted runner, and never claims a
> reproduction it cannot back with evidence.

```text
GitHub Actions fails
     ↓
Replico — REPRODUCE IT      (which job/step, what environment, replay locally)
     ↓
WhyFail — EXPLAIN IT        (structured root-cause diagnosis of the Python failure)
     ↓
SecretShield — PROTECT IT   (redaction of every displayed or saved value)
```

---

## The problem

```text
Developer pushes code        →  CI fails  →  Developer reads logs
      →  Guesses what went wrong  →  Changes code  →  Pushes again  →  CI fails again
```

## How it works

```text
GitHub Actions fails
        ↓
replico <run-url>                    # or: replico run <run-id>; `rep` works too
        ↓
Analyze the failed workflow
        ↓
Reconstruct the relevant environment
        ↓
Run the failing command locally
        ↓
      ┌────────┴────────┐
   FAILED            PASSED
      ↓                   ↓
WhyFail diagnosis   NOT_REPRODUCED (local pass)
(if Python failure)
      ↓
SecretShield redaction → display + .replico/ artifacts
        ↓
REPRODUCED ✓ / NOT REPRODUCED ✗ / PARTIAL ⚠   (with evidence and parity)
        ↓
.replico/ saved — iterate: fix code → replico rerun
```

## Quick start

```bash
pip install replico

# from the checkout of the repository whose CI failed:
replico https://github.com/example/project/actions/runs/123456789
```

That's it. No account, no API key, no cloud service. Public repositories work
without any token. Private repositories need either `GITHUB_TOKEN` in your
environment or the GitHub CLI (`gh auth login`).

GitHub's API only serves **job log downloads to authenticated requests**, even
for public repositories. Without a token Replico still finds the failed run,
job and step, fetches the workflow YAML at the exact commit, and reproduces
from the workflow — but the log-level failure analysis needs a token:

```bash
export GITHUB_TOKEN=ghp_...   # or: gh auth login
```

Example output (abridged):

```text
✓ failed job: Python 3.13
✓ failed step: Run tests

REPLICO REPRODUCTION PLAN

Repository: example/project
Commit:     a82f91c
Workflow:   Tests
Job:        Python 3.13
Failed step: Run tests
Runner:     ubuntu-24.04
Ecosystem:  python

Detected setup:
  ✓ checkout
  ✓ Python 3.13
  • pip install -r requirements.txt

Reproducing...
✓ environment ready
✓ dependencies installed
✗ the failing command FAILED locally (exit 1, 2.4 s)

REPLICO RESULT — CI FAILURE REPRODUCED
  • the same failing test(s) reproduced locally: tests/test_auth.py::test_login

WHYFAIL DIAGNOSIS
─────────────────────────────────────────────────────────────
Immediate failure:
    response["user"] raised KeyError

Likely cause:
    The mapping does not contain the key "user".

Expected:
    the mapping accessed by `response` contains the key 'user'

Actual:
    the available keys are: 'account', 'status'

Confidence:
    HIGH

What to investigate:
  • Verify what `response` really contains when this code runs.
  • Guard the access if the key can legitimately be absent.

Evidence: observed locally during reproduction (CI evidence is separate)
```

After a code change:

```bash
replico rerun
```

```text
REPLICO RERUN
✓ environment ready
✓ running the previously failing step…

  ✓ the failing command PASSED locally

REPLICO RESULT — CI FAILURE NOT REPRODUCED
  • the previously reproduced failure no longer occurs locally.
```

(Wording is careful on purpose: a local pass does *not* prove CI will pass.)

## Installation

Requirements: **Python 3.11+**, `git`. Docker is optional but recommended
when the CI runner OS differs from your machine.

```bash
pip install replico        # installs the `replico` command
# or from source:
pip install -e ".[dev]"    # development install
```

SecretShield (`secretshield>=0.4.2`) is a real dependency: every log line,
environment value, command output and artifact that Replico displays or saves
passes through SecretShield's detection/redaction (see [Security](#security)).
WhyFail (`whyfail>=3.0.0`) is a real dependency too: it diagnoses reproduced
Python failures locally (see [WhyFail integration](#whyfail-integration)).

## Usage

```bash
# Reproduce a failed run (the flagship command — same as `replico reproduce …`)
replico https://github.com/owner/repo/actions/runs/123456789
replico reproduce https://github.com/owner/repo/actions/runs/123456789
replico run 123456789              # run id; repository read from git origin

# Iterate after code changes
replico rerun                      # re-run the saved reproduction
replico status                     # saved state vs current checkout
replico diff                       # what changed since the CI failure

# Diagnose the saved reproduction with WhyFail (offline, no GitHub)
replico diagnose                   # re-runs the failing command under WhyFail
replico diagnose --whyfail         # same (WhyFail is the only engine)

# Inspect
replico env                        # sanitized local environment fingerprint
replico config                     # effective configuration (no secrets)
replico version

# Hygiene
replico clean                      # remove .replico/ (confirmed)

# Inside CI (e.g. under `if: failure()`), capture context for later:
replico capture
```

Common flags:

| Flag | Meaning |
| --- | --- |
| `--job <job>` | which failed job to reproduce (multi-job runs) |
| `--step <name>` | which step to reproduce (default: the failing step) |
| `--docker` / `--no-docker` | force / forbid Docker isolation |
| `--diagnose` / `--no-diagnose` | force / skip WhyFail diagnosis (default: auto for Python failures) |
| `--offline` | use only locally saved data — no GitHub requests |
| `--json` | machine-readable JSON on stdout (all prose goes to stderr) |
| `--plain` | no colors, no decorations (CI/log capture) |
| `--yes` | accept confirmations non-interactively |
| `--verbose` / `--debug` | more detail (still redacted) |
| `--clean` | remove an existing `.replico/` before reproducing |

### Multiple failed jobs

```text
2 failed jobs found.

1. test-python
2. integration-linux

Use --job to select one:
replico <run-url> --job test-python
```

When exactly one job failed it is selected automatically. The same logic
applies inside the job: the first failed step is chosen.

### Matrix workflows

Matrix combinations are matched back to the workflow YAML (best effort:
job id, explicit `name:`, and matrix-expanded display names). The matching
combination's variables are used when rendering the steps.

## Supported workflows (v0.2)

| Ecosystem | Status |
| --- | --- |
| Python (`actions/setup-python`, `pip`, `pytest`, `python -m unittest`, …) | **supported** (+ WhyFail diagnosis) |
| Plain shell jobs (`run:` only, no package managers) | supported (generic) |
| Node.js / Go / Rust / Java / .NET | detected, reported as *unsupported* (roadmap v0.3) |
| Failing step is a third-party `uses:` action | reported as *unsupported* |

Replico understands checkout/setup actions, dependency install commands
(`pip install`, `-r` requirements, `pip install -e .`), `python -m pytest`,
environment blocks, `working-directory`, `defaults.run.shell`, `strategy`
matrices, and `${{ matrix.* }}` / basic `${{ github.* }}` expressions.
Secrets referenced as `${{ secrets.X }}` are never fetched; steps that
genuinely require them will fail locally in a deterministic way and Replico
will say so.

## WhyFail integration

Replico uses **WhyFail** (`whyfail>=3.0.0`) to analyze Python failures after
they have been reproduced locally. WhyFail provides evidence-based
root-cause diagnostics; Replico remains responsible for CI analysis and
reproduction. The three tools stay independent and usable on their own:

```text
Replico     = reproduction   (turn the CI failure into a local failure)
WhyFail     = diagnosis      (explain the reproduced Python failure)
SecretShield = protection    (redact every piece of sensitive evidence)
```

How diagnosis works:

* **When.** A diagnosis is attempted automatically when a reproduction (or
  `rerun`) *fails locally*, the job is a Python job, and the failing step is
  a python/pytest invocation WhyFail can run — `python <script>`,
  `python -m <module>` or `pytest …`. Nothing runs for successful commands,
  non-Python failures, Docker executions, or `python -c` steps (WhyFail 3.x
  cannot diagnose those — Replico says so and moves on).
* **How.** The same failing command is re-run under WhyFail's structured CLI
  in the reproduction environment; Replico consumes the real
  `Diagnostic.to_dict()` schema (failure, likely cause, expected/actual,
  broken assumptions, confidence, suggestions, call chain) instead of
  scraping terminal output.
* **Where the evidence comes from.** WhyFail analyzes the *locally
  reproduced failure* — never the CI runner's live runtime. Output is
  labeled accordingly: CI evidence, local reproduction evidence, WhyFail
  local diagnosis. Replico never claims WhyFail proved the CI root cause.
* **`replico diagnose`** re-runs the saved failing command under WhyFail on
  demand (`--offline` supported — no GitHub access involved).
* **Controls.** `--diagnose` forces diagnosis when applicable,
  `--no-diagnose` disables it, and `.replico.toml` can switch it off
  globally:

```toml
[diagnostics]
enabled = true   # master switch (diagnosis only — redaction stays mandatory)
whyfail = true   # use the WhyFail engine
```

* **Output.** In `--json` mode the result document gains a `whyfail` block:

```json
{
  "whyfail": {
    "tool": "whyfail",
    "version": "3.0.0",
    "available": true,
    "diagnosed": true,
    "source": "local_reproduction",
    "exit_code": 1,
    "diagnostics": [ { "whyfail": 1, "schema": 2, "exception_type": "KeyError", "diagnosis": { } } ]
  }
}
```

  When no diagnosis exists the block says so explicitly
  (`"diagnosed": false` with a `reason` such as
  `"unsupported_failure_type"`); Replico never claims a diagnosis that does
  not exist. A structured, sanitized copy is always saved to
  `.replico/whyfail.json` (present only when a diagnosis was attempted).

If WhyFail is ever missing or fails, reproduction still works: Replico
reports that the diagnosis is unavailable and keeps the reproduction verdict
intact.

## Result states

Replico distinguishes — and never conflates:

| State | Meaning |
| --- | --- |
| `reproduced` | the failing command failed locally **and** the failure signature (failing test id / error category) matches CI |
| `partially_reproduced` | a failure occurred locally but its identity could not be confirmed against CI, **or** the local run passed under materially different conditions |
| `not_reproduced` | the failing command passed locally under adequate environment parity |
| `unsupported` | Replico does not yet know how to reproduce this workflow |

WhyFail runs only against Python failures reproduced locally (exit `1` /
verdict `reproduced`); a local pass or an unrelated failure simply skips it
with an honest note.

Verbal results are matched by stable exit codes:

| Code | Meaning |
| --- | --- |
| `0` | reproduction succeeded — nothing is failing locally (verdict `not_reproduced`; a `rerun` that now passes) |
| `1` | reproduced failure still exists (verdict `reproduced`; a `rerun` that still fails) |
| `2` | could not reproduce (blocked, or `partially_reproduced` without a local failure) |
| `3` | invalid input (bad URL, unknown `--job`, missing args) |
| `4` | authentication problem (private repo without a usable token) |
| `5` | unsupported workflow |
| `6` | environment/setup problem (missing tool, venv/Docker failure) |
| `70` | internal error |

## Environment parity

Replico fingerprints your machine (`replico env`) and compares it with the CI
job:

```text
ENVIRONMENT DIFFERENCES
  ✓ OS Windows 10/11: CI ubuntu-24.04  ← mismatch would be ✗ / a Docker hint
  ✗ Python 3.13: using 3.12.4
  ✓ git
  ✓ dependencies
  ✓ environment variables
Environment parity: 72% (estimate — not a guarantee)
```

Parity is a transparent, weighted heuristic (OS, Python version, isolation,
git, dependencies, env vars) — not a claim of byte-for-byte parity with
GitHub's runner images. When parity is low and the local run passed, Replico
will not let you claim the failure is gone.

## Isolation

* **Local (default, Python jobs):** dependencies are installed into a virtual
  environment under `.replico/venv`, never into your global environment.
* **`--docker`:** the repository is mounted into a matching image
  (`python:3.13-slim`, `ubuntu:24.04`, …) — the closest match for Linux
  runners and the recommended mode when CI ran on a different OS.
* Automatic mode picks Docker when the CI runner OS (or requested Python
  version) is not available locally and Docker is running.

Replico never runs `sudo`, administrator commands or destructive filesystem
operations without explicit confirmation. Commands extracted from workflow
files are audited first (`replico/security/guard.py`); risky ones require
`--yes` or an interactive confirm, and elevation is never performed for you.

## Security

Replico is local-first and privacy-conscious:

* Network access is limited to the GitHub API (plus dependency downloads the
  workflow itself requests). **No source code, logs, environment values or
  artifacts are uploaded anywhere. There is no telemetry.**
* Tokens come from `GITHUB_TOKEN` / `GH_TOKEN` / the GitHub CLI and travel
  only in the `Authorization` header of API requests. They are never logged,
  displayed or saved.
* **SecretShield** (`secretshield>=0.4.2`) is used wherever sensitive content
  could appear:
  * CI logs and command output are scanned/redacted before display or
    persistence (`redact`/`detect`),
  * `secretshield.enable()` protects `stdout`/`stderr` and the logging module
    as a last line of defense,
  * `replico/security/redaction.py` is the single adapter between Replico and
    SecretShield; Replico adds *literal* known-secret redaction (values from
    your environment) on top, because SecretShield is pattern/entropy based
    and cannot know that a low-entropy string is your password.
* Secret-like environment variables are shown as `NAME = present` — never
  their values — in `replico env`, fingerprints, JSON output, `--debug`, and
  everything saved under `.replico/`.
* Environment values are kept out of child-process environments unless they
  are workflow literals that CI itself would set; `${{ secrets.* }}` is never
  resolved or injected.
* **WhyFail diagnostics are double-redacted.** WhyFail redacts runtime
  values with its own SecretShield layer, and every diagnostic is passed
  through Replico's `Sanitizer` again before it can reach the console,
  `--json` output, `reproduction.json` or `.replico/whyfail.json`. WhyFail
  runs locally, fully offline, in the reproduction environment.
* Malicious inputs are handled defensively: YAML is parsed with a
  budgeted/memoized engine (alias-expansion bombs are neutralized),
  repository/job names are validated before touching paths or URLs, command
  lines are audited, subprocesses are spawned without `shell=True` for
  Replico's own commands, and env var names are validated.
* Workflow YAML is stored redacted under `.replico/`; see
  `replico/security/` and the tests in `tests/` for the details.

## Privacy

Your repository stays on your machine. Replico makes no network calls beyond
GitHub API requests that are required to read the run, its logs and its
workflow file, plus whatever the workflow itself runs (dependency installs).
There is no Replico server, no account, and telemetry is not collected — if
telemetry is ever introduced it will be opt-in only.

## Limitations (honest)

* Replico does **not** clone GitHub's runner images. Tools preinstalled on
  GitHub-hosted runners (compilers, system libraries, caches) are generally
  absent locally; parity numbers reflect that.
* Only `run:` steps are replayed. Third-party actions cannot be executed
  locally without their container/runtime.
* v0.2 covers Python workflows well and plain shell jobs; Node/Go/Rust are
  detected and reported as unsupported rather than half-executed.
* WhyFail diagnosis applies to Python failures only, needs a python/pytest
  invocation WhyFail can run (`python <script>`, `python -m <module>`,
  `pytest …` — not `python -c`), and requires the failure to reproduce
  locally; anything else is skipped with an honest note. Diagnosis re-runs
  the failing command, so it costs one extra local execution.
* Log analysis is heuristic. When Replico cannot extract a confident failure
  signature it says so instead of guessing.
* Multi-line steps are replayed as one script (matching GitHub's behavior)
  with the shell GitHub would use (`bash -eo pipefail`, pwsh on Windows).

## Architecture

```text
replico/
├── cli.py            argparse entry point, exit-code mapping
├── flows.py          reproduce / rerun orchestration
├── pipeline.py       run → plan → execute → verdict engine helpers
├── cmds.py           status / diff / env / clean / config / capture /
│                     diagnose
├── config.py         .replico.toml (optional) + defaults
├── ui.py             safe console output (rich, sanitized, JSON mode)
├── errors.py         exceptions bound to stable exit codes
├── github/           URL parsing, REST client (token-safe), job/step models
├── workflow/         bomb-safe YAML parser, workflow model, job matcher,
│                     environment/dependency detection
├── environments/     ecosystem adapters (base, python), fingerprinting
├── execution/        shell runner, Docker isolation
├── analysis/         log analysis (500 lines → 12 relevant), classifier,
│                     WhyFail adapter (structured diagnosis)
├── storage/          .replico/ store (redacted artifacts + whyfail.json)
└── security/         SecretShield adapter, sanitizer, command/path guards
```

Ecosystems plug in behind `EcosystemAdapter`:

```python
class EcosystemAdapter(ABC):
    def detect(self, analysis: JobAnalysis) -> EcosystemDetection: ...
    # see environments/base.py — Node (planned v0.3) already registers
```

## Development

```bash
pip install -e ".[dev]"
pytest                    # offline test suite (mocked GitHub)
ruff check . && ruff format --check .
mypy src/replico
python -m build           # package validation
```

## Replico's own CI

`.github/workflows/ci.yml` tests Replico itself on Windows/Ubuntu/macOS and
Python 3.11–3.14, running tests, lint, type checks, build and package
validation. Dogfooding goal: Replico should eventually reproduce its own CI
failures (`replico capture` in a `if: failure()` step is the first step).

## Roadmap

* **v0.1** — GitHub Actions (public repos), failed job/step detection,
  Python reproduction, honest verdicts, `.replico/`, Windows / Linux /
  macOS, offline test suite, SecretShield integration.
* **v0.2 (this release)** — WhyFail integration: automatic structured
  diagnosis of reproduced Python failures, `replico diagnose`, `--diagnose` /
  `--no-diagnose`, richer failure classification, `whyfail.json` artifacts,
  status/diff diagnosis awareness, Docker-reasoning improvements.
* **v0.3** — Node.js, Go, Rust adapters, matrix/multi-job refinements,
  deeper failure classification, private-repo-first polish.
* **v0.4+** — GitHub Action, PR comments, reproduction artifacts, local
  failure history, IDE integrations.

## Contributing

Issues and pull requests welcome. Before contributing, read the security
model (`replico/security/`) — secret safety is non-negotiable. All tests must
run offline; GitHub interactions are mocked.

## License

MIT — see [LICENSE](LICENSE).
