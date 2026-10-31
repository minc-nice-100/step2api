#!/usr/bin/env python3
"""Backwards-compatible launcher. Prefer `python -m step2api` or the `step2api` console script."""

from step2api.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
