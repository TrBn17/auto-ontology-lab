"""Module execution entrypoint for `python -m lab`."""

from __future__ import annotations

from lab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
