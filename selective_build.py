#!/usr/bin/env python3
"""Backward-compatible wrapper for the optimizer package."""

from __future__ import annotations

from optimizer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
