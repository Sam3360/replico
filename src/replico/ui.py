"""Output layer.

Every string that reaches the terminal first passes through the sanitizer so
that debug/verbose output respects redaction. All rendering uses plain
``rich.text.Text`` (never markup parsing) so hostile log content cannot
inject formatting.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from replico.security.redaction import Sanitizer


class UI:
    """A tiny, safe console facade.

    Modes:
    * human  — progress + results on stdout, extras muted on stderr
    * json   — the machine-readable document goes to stdout; all human text
      goes to stderr so stdout stays valid JSON
    * plain  — no color / decoration (also used for CI/log capture)
    """

    def __init__(
        self,
        *,
        plain: bool = False,
        json_mode: bool = False,
        sanitizer: Sanitizer | None = None,
        verbose: bool = False,
        assume_yes: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.plain = plain or json_mode
        self.json_mode = json_mode
        self.sanitizer = sanitizer or Sanitizer(enabled=False)
        self.verbose = verbose
        self.assume_yes = assume_yes
        self._out_console = Console(
            file=stdout or sys.stdout, color_system=None if self.plain else "auto"
        )
        self._err_console = Console(
            file=stderr or sys.stderr, color_system=None if self.plain else "auto"
        )

    # -- sanitization ---------------------------------------------------------

    def _safe(self, text: str) -> str:
        return self.sanitizer.redact(text)

    # -- output primitives ----------------------------------------------------

    def _emit(
        self, console: Console, text: str, style: str | None = None, to_out: bool = True
    ) -> None:
        safe = self._safe(text)
        target = self._out_console if (to_out and not self.json_mode) else self._err_console
        target.print(Text(safe, style=style or ""), highlight=False, soft_wrap=True)

    def out(self, text: str, style: str | None = None) -> None:
        self._emit(self._out_console, text, style, to_out=True)

    def info(self, text: str) -> None:
        self._emit(self._err_console, text, "dim", to_out=False)

    def status(self, text: str) -> None:
        """Progress line (stderr in json mode; info channel otherwise)."""
        if self.json_mode or self.verbose:
            self._emit(self._err_console, text, "dim", to_out=False)
        else:
            self._emit(self._err_console, f"• {text}", "dim", to_out=False)

    def ok(self, text: str) -> None:
        self._emit(self._out_console, f"✓ {text}", "green")

    def warn(self, text: str) -> None:
        self._emit(self._out_console, f"⚠ {text}", "yellow")

    def error(self, text: str) -> None:
        self._emit(self._out_console, f"✗ {text}", "red")

    def section(self, title: str) -> None:
        self._emit(self._out_console, "", to_out=not self.json_mode)
        self._emit(self._out_console, title.upper(), "bold cyan")

    def rule(self, title: str = "") -> None:
        console = self._out_console if not self.json_mode else self._err_console
        console.rule(title, style="dim")

    # -- key/value lines ------------------------------------------------------

    def kv(self, key: str, value: str, style: str | None = None) -> None:
        safe_key = self._safe(key)
        safe_value = self._safe(value)
        console = self._out_console if not self.json_mode else self._err_console
        console.print(
            Text(f"{safe_key}: ", style="bold") + Text(safe_value, style=style or "default"),
            highlight=False,
        )

    # -- machine-readable output ----------------------------------------------

    def print_json(self, doc: dict[str, Any]) -> None:
        text = json.dumps(doc, indent=2, default=str)
        print(self._safe(text), file=sys.stdout)

    # -- confirmation ---------------------------------------------------------

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ask for confirmation. Returns False without blocking when stdin is
        not a terminal (use --yes to proceed in scripts/CI)."""
        if self.assume_yes:
            return True
        if self.json_mode:
            return False
        try:
            interactive = sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            interactive = False
        if not interactive:
            return default
        suffix = "[Y/n] " if default else "[y/N] "
        try:
            answer = input(f"{self._safe(prompt)} {suffix}")
        except (EOFError, KeyboardInterrupt):
            return default
        answer = answer.strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")


def humanize_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{seconds / 60:.1f} min"
