# Security

Replico executes commands found in *other people's* workflow YAML files, on
your machine, next to your source tree. That makes it a small remote-code
execution engine by design — so this document spells out the threat model,
the controls in place, and how to report problems.

## Threat model

What Replico is asked to do, by construction, is risky:

1. It reads **workflow YAML authored by whoever controls the repository**
   whose CI failed. A hostile repository can contain arbitrary `run:` steps.
2. It reads **GitHub API responses** (run metadata, job logs) — hostile or
   compromised content is possible.
3. It **executes commands locally** (or in Docker) to reproduce a failure.
4. It **persists artifacts** under `.replico/` that may echo log or
   environment content.

Replico's job is to make steps 1–4 safe enough that a developer can point it
at an untrusted public repository without fear.

## Guarantees

### Diagnosis is local and double-sanitized

* When a reproduced Python failure is diagnosed, **WhyFail runs locally and
  offline** as a subprocess under Replico's interpreter, analyzing the
  reproduction environment's runtime (never the CI runner). Its structured
  diagnostics are redacted by WhyFail's own SecretShield layer and then
  re-sanitized by Replico's `Sanitizer` before anything is displayed or
  written to `.replico/whyfail.json` / `reproduction.json` / JSON output.
  The `whyfail.json` artifact is written only when a diagnosis was actually
  attempted, and never claims a diagnosis that does not exist.

### Secrets never leave the machine, and rarely leave the code path

* **No secret is ever displayed or persisted in plain form.** Every UI line,
  JSON payload and `.replico/` artifact passes through `Sanitizer`
  (`replico/security/redaction.py`), which layers **SecretShield**
  (pattern + high-entropy detection) over literal redaction of *known*
  secret values collected from the environment/config. Debug and verbose
  output respect the same pipeline.
* **Environment values are shown as presence only.** `API_KEY = present`,
  never `API_KEY = abc…` (see `replico env`).
* **GitHub tokens are resolved from** `GITHUB_TOKEN` / `gh auth token` and
  used only as an `Authorization` header over HTTPS. They are never logged,
  echoed in commands, or written to artifacts. URLs containing embedded
  credentials are refused and reported.
* No repository contents, logs, environment or artifacts are uploaded
  anywhere. Network use is limited to GitHub API access and dependency
  installation.

### Commands are audited before execution

`replico/security/guard.py` scans every workflow `run:` script for:

* **elevation** — `sudo`, `doas`, `su`, Windows `runas`/`gsudo`,
  `Start-Process -Verb RunAs`;
* **destructive operations** — recursive `rm` at the filesystem root,
  `mkfs`/`format`, `dd` to block devices, writes to `/dev/*`, shutdown/
  reboot, `chmod`/`chown` of `/`, `git push`/`reset --hard`/`clean -f`;
* **network execution** — `curl … | sh`, PowerShell `iwr | iex`;
* **suspicious patterns** — `eval $(…)`, base64/xxd decode pipelines,
  fork bombs.

Findings are surfaced in the reproduction plan and execution **requires
explicit interactive confirmation** (or `--yes` after the full plan has been
shown). Replico never runs `sudo` or administrator-level commands on its own.

### Hostile input is handled defensively

* **Workflow YAML** is parsed with an explicit node budget, depth cap and
  memoized alias expansion, so billion-laughs / alias-chain / deep-nesting
  documents fail fast instead of exhausting memory or the stack
  (`replico/workflow/parser.py`).
* **Repository, owner and job names** are validated against a strict
  character set before they can reach a URL, filesystem path or subprocess
  argument. Paths built from untrusted names go through `safe_join`, which
  refuses anything escaping the target directory.
* **Environment variables** handed to subprocesses have their names
  validated; hostile names are dropped.
* **Subprocesses never use `shell=True`.** Scripts are written to temp
  files and executed through an explicit, probed interpreter
  (`replico/execution/runner.py`).

### Isolation when it matters

`--docker` (or `--no-docker` to force local) runs the reproduction inside a
container image matched to the runner OS when practical. Local execution is
the fallback when Docker is absent, but destructive/elevated commands still
require confirmation there.

## Reporting a vulnerability

Please report security issues privately rather than in a public issue:

* Open a GitHub Security Advisory, or
* Email the maintainers (address linked from the repository profile).

Include: the affected version, a minimal reproduction, and — for leakage
bugs — exactly which channel leaked (stdout, JSON, artifact, log) and the
input shape. Secret-leakage reports are treated as the highest priority.

## Verification

The offline test suite covers the security surface:

* secret redaction (API keys, tokens, passwords, private keys,
  high-entropy values) across UI / JSON / `.replico/` artifacts /
  debug output — `tests/test_security_redact.py`;
* WhyFail integration security — secrets printed by a failing command never
  survive in diagnostics, `whyfail.json`, reproduction artifacts or console
  output — `tests/test_whyfail_adapter.py`,
  `tests/test_integration_whyfail.py`;
* command auditing and guards — `tests/test_security_guard.py`;
* malicious YAML (billion laughs, aliases, deep nesting),
  injection, traversal, hostile filenames/env — spread through the suite.
