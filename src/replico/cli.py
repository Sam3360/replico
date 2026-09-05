"""Command line interface.

Exit codes are stable and documented (see replico/errors.py):

    0  reproduction succeeded (no failure reproduced locally)
    1  reproduced failure still exists
    2  could not reproduce
    3  invalid input
    4  authentication problem
    5  unsupported workflow
    6  environment/setup problem
    70 internal error
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import traceback

from replico import __version__
from replico.cmds import (
    cmd_capture,
    cmd_clean,
    cmd_config,
    cmd_diagnose,
    cmd_diff,
    cmd_env,
    cmd_status,
    parse_ref_token,
)
from replico.config import Config, load_config
from replico.errors import EXIT_INTERNAL, InvalidInputError, ReplicoError
from replico.flows import build_app, reproduce, rerun
from replico.pipeline import Outcome, ReproOptions
from replico.security.redaction import Sanitizer, enable_global_protection
from replico.ui import UI

COMMANDS = (
    "reproduce",
    "run",
    "rerun",
    "diagnose",
    "status",
    "diff",
    "env",
    "clean",
    "config",
    "capture",
    "version",
    "help",
)

log = logging.getLogger("replico")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verbose", action="store_true", help="more progress detail")
    parser.add_argument("--debug", action="store_true", help="debug logging (redacted)")
    parser.add_argument("--plain", action="store_true", help="no colors / decorations")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--yes", action="store_true", help="accept confirmations")
    parser.add_argument(
        "--no-telemetry",
        action="store_true",
        help="accepted for clarity (replico has no telemetry)",
    )
    parser.add_argument(
        "--offline", action="store_true", help="use only locally saved data (no GitHub requests)"
    )


def _add_reproduce_args(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument("target", nargs="?", help="GitHub Actions run URL or numeric run id")
    parser.add_argument("--job", help="job to reproduce (default: the failed job)")
    parser.add_argument("--step", help="step to reproduce (default: the failed step)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--docker", dest="docker", action="store_true", help="force Docker-based reproduction"
    )
    group.add_argument(
        "--no-docker", dest="docker", action="store_false", help="force local reproduction"
    )
    parser.set_defaults(docker=None)
    parser.add_argument("--keep", action="store_true", help="keep generated scripts (debugging)")
    parser.add_argument(
        "--clean",
        dest="clean_first",
        action="store_true",
        help="remove any previous .replico/ before reproducing",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--diagnose",
        dest="diagnose",
        action="store_true",
        help="run WhyFail against a reproduced Python failure",
    )
    group.add_argument(
        "--no-diagnose",
        dest="diagnose",
        action="store_false",
        help="skip automatic WhyFail diagnosis",
    )
    parser.set_defaults(diagnose=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replico",
        description="Turn GitHub Actions CI failures into locally reproducible failures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"replico {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p_reproduce = sub.add_parser(
        "reproduce",
        help="reproduce a failed GitHub Actions run locally (default command)",
        description=(
            "Analyze a failed GitHub Actions run, reconstruct its environment "
            "and run the failing step locally. `replico <run-url>` is the same "
            "command."
        ),
    )
    _add_reproduce_args(p_reproduce)

    p_run = sub.add_parser(
        "run",
        help="shorthand: replico run <run-id>",
        description="Same as `replico reproduce <run-id>` using the current "
        "repository's origin remote.",
    )
    _add_reproduce_args(p_run)

    p_rerun = sub.add_parser("rerun", help="re-run a saved reproduction after code changes")
    _add_common(p_rerun)
    p_rerun.add_argument("--keep", action="store_true", help="keep generated scripts")
    p_rerun.add_argument(
        "--diagnose",
        dest="diagnose",
        action="store_true",
        help="run WhyFail against the reproduced failure",
    )
    p_rerun.add_argument(
        "--no-diagnose",
        dest="diagnose",
        action="store_false",
        help="skip automatic WhyFail diagnosis",
    )
    p_rerun.set_defaults(diagnose=None)

    p_diagnose = sub.add_parser(
        "diagnose",
        help="diagnose the most recently reproduced local failure with WhyFail",
        description=(
            "Re-runs the saved failing command under WhyFail and renders the "
            "structured diagnosis. Fully offline; needs a saved reproduction "
            "(.replico/) from `replico <run-url>` or `replico rerun`."
        ),
    )
    _add_common(p_diagnose)
    p_diagnose.add_argument(
        "--whyfail",
        action="store_true",
        help="use WhyFail — the only diagnostic engine (accepted for clarity)",
    )

    for name, doc in (
        ("status", "show the state of the saved reproduction"),
        ("diff", "show what changed since the reproduced CI failure"),
        ("env", "print a sanitized local environment fingerprint"),
        ("clean", "remove the saved .replico/ reproduction"),
        ("config", "print the effective configuration (no secrets)"),
    ):
        _add_common(sub.add_parser(name, help=doc))

    p_capture = sub.add_parser(
        "capture",
        help="capture CI failure context into .replico/ (for use inside CI)",
    )
    _add_common(p_capture)
    p_capture.add_argument("--job", help="job name that failed (default: $GITHUB_JOB)")
    p_capture.add_argument("--step", help="step name that failed")

    sub.add_parser("version", help="print the Replico version")
    sub.add_parser("help", help="show this help")

    parser.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    return parser


def _find_command(argv: list[str]) -> tuple[str, list[str]]:
    """Locate the subcommand token; bare URLs / run ids mean `reproduce`."""
    if not argv:
        return "", []
    # A URL can contain '://' but never starts with '-'.
    for index, token in enumerate(argv):
        if token.startswith("-"):
            continue
        if token in COMMANDS:
            rest = argv[:index] + argv[index + 1 :]
            return token, rest
        break
    return "reproduce", argv


def _configure_logging(cfg: Config, verbose: bool, debug: bool) -> None:
    level = (
        logging.DEBUG
        if debug
        else (logging.INFO if (verbose or cfg.output.verbose) else logging.WARNING)
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
    root = logging.getLogger("replico")
    root.setLevel(level)
    root.handlers[:] = [handler]
    root.propagate = False


def _run(argv: list[str]) -> int:
    command, rest = _find_command(argv)

    cfg = load_config()
    if "--version" in rest and command not in ("version", "help"):
        print(f"replico {__version__}")
        return 0
    if command == "":
        command = "help"
    if command in ("help",):
        build_parser().print_help()
        return 0
    if command == "version":
        print(f"replico {__version__}")
        return 0

    parser = build_parser()
    subparser_names = {
        action.dest: action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    }
    subparser = subparser_names["command"].choices[command]
    try:
        args = subparser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0

    # Effective flags: CLI overrides config file.
    plain = args.plain or cfg.output.plain
    json_mode = bool(args.json)
    verbose = bool(args.verbose) or cfg.output.verbose
    debug = bool(args.debug) or cfg.output.debug
    _configure_logging(cfg, verbose, debug)

    sanitizer = Sanitizer(
        enabled=cfg.security.redact_secrets,
        mask=cfg.security.mask_with,
        entropy_threshold=cfg.security.entropy_threshold,
    )
    if cfg.security.redact_secrets:
        enable_global_protection(sanitizer)

    ui = UI(
        plain=plain,
        json_mode=json_mode,
        sanitizer=sanitizer,
        verbose=verbose,
        assume_yes=bool(args.yes),
    )

    if command == "config":
        outcome = cmd_config(cfg)
    else:
        ctx = build_app(cfg, ui, sanitizer)
        if command == "status":
            outcome = cmd_status(ctx)
        elif command == "diff":
            outcome = cmd_diff(ctx)
        elif command == "env":
            outcome = cmd_env(ctx)
        elif command == "clean":
            outcome = cmd_clean(ctx)
        elif command == "capture":
            outcome = cmd_capture(ctx, getattr(args, "job", None), getattr(args, "step", None))
        elif command == "diagnose":
            outcome = cmd_diagnose(ctx, whyfail_flag=bool(getattr(args, "whyfail", False)))
        else:  # reproduce | run | rerun
            options = ReproOptions(
                job=getattr(args, "job", None),
                step=getattr(args, "step", None),
                docker=getattr(args, "docker", None),
                offline=bool(args.offline),
                keep=bool(args.keep),
                clean_first=bool(getattr(args, "clean_first", False)),
                assume_yes=bool(args.yes),
                diagnose=getattr(args, "diagnose", None),
            )
            if command == "rerun":
                outcome = rerun(ctx, options)
            else:
                target = getattr(args, "target", None)
                if target is None:
                    # `replico run` may read the run id from the environment.
                    target = _env_run_id()
                if target is None:
                    raise InvalidInputError(
                        "missing run — usage: replico <github-actions-run-url>  "
                        "or  replico run <run-id>"
                    )
                ref = parse_ref_token(target, ctx)
                outcome = reproduce(ctx, ref, options)

    if json_mode:
        ui.print_json(_outcome_doc(outcome))
    return outcome.exit_code


def _env_run_id() -> str | None:
    import os

    value = os.environ.get("GITHUB_RUN_ID", "").strip()
    return value or None


def _outcome_doc(outcome: Outcome) -> dict:
    if outcome.doc:
        return outcome.doc
    return {"tool": "replico", "status": "ok", "exit_code": outcome.exit_code}


def _force_utf8() -> None:
    """Windows consoles/redirects often default to cp1252; rich prints unicode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _force_utf8()
    try:
        return _run(argv)
    except ReplicoError as exc:
        sanitizer = Sanitizer()
        ui = UI(
            plain="--plain" in argv or "--json" in argv,
            json_mode="--json" in argv,
            sanitizer=sanitizer,
        )
        ui.error(exc.message)
        if exc.hint:
            ui.out(f"  hint: {exc.hint}", style="dim")
        code = exc.exit_code
        if "--json" in argv:
            ui.print_json(
                {
                    "tool": "replico",
                    "status": "error",
                    "error": {"code": code, "message": sanitizer.redact(exc.message)},
                    "exit_code": code,
                }
            )
        return code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - final safety net
        if "--debug" in argv:
            traceback.print_exc()
        else:
            sys.stderr.write(
                f"replico: internal error: {exc.__class__.__name__}: "
                f"{Sanitizer().redact(str(exc))}\n"
                "re-run with --debug for a traceback and report the bug.\n"
            )
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
