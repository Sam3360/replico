"""Allow `python -m replico`."""

from replico.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
