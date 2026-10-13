#!/usr/bin/env python3
"""Read-only wrapper for Katcha production release evidence verification."""
from katcha.ops.release_eligibility import main

if __name__ == "__main__":
    raise SystemExit(main())
