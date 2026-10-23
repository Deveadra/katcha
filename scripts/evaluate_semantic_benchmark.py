#!/usr/bin/env python3
"""Evaluate a frozen semantic split without invoking providers or acquiring media."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from katcha.editorial.semantic_benchmark import (
    BenchmarkManifest,
    BenchmarkPredictions,
    evaluate_semantics,
)

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Benchmark document exceeds the offline evaluation size limit")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--manifest-sha256",
        required=True,
        help="Previously locked canonical manifest digest, not a new digest to trust",
    )
    parser.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = BenchmarkManifest.model_validate_json(read_bounded(args.manifest))
        if manifest.sha256 != args.manifest_sha256:
            raise ValueError("Manifest changed since the benchmark was locked")
        predictions = BenchmarkPredictions.model_validate_json(read_bounded(args.predictions))
        report = evaluate_semantics(manifest, predictions)
    except (OSError, ValueError) as exc:
        # Do not echo invalid documents, prompts, credentials or upstream content.
        parser.exit(
            2,
            f"Benchmark validation failed ({type(exc).__name__}); "
            "check document schema, locked digest, complete split and media scope.\n",
        )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
