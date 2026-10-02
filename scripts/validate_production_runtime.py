#!/usr/bin/env python3
"""Validate the hosted production environment without exposing secret values."""

from katcha.ops.production_runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
