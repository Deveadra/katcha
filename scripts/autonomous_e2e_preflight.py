#!/usr/bin/env python3
"""Read-only M00 source inventory. No provider inference, media transfer or DB writes."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import subprocess
from pathlib import Path

from katcha.services.retrieval_diagnostics import (
    retrieval_capabilities,
    retrieval_capabilities_checked,
)

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    "discovery": [
        "src/katcha/services/source_polling.py",
        "src/katcha/services/discovery_polling.py",
    ],
    "activation": [
        "src/katcha/services/trend_auto_activation.py",
        "src/katcha/services/trend_editorial_dispatch.py",
    ],
    "acquisition": [
        "src/katcha/services/acquisition_jobs.py",
        "src/katcha/services/acquisition_handoff.py",
    ],
    "editorial": [
        "src/katcha/orchestration/editorial_workflows.py",
        "src/katcha/services/editorial_runs.py",
    ],
    "requirements": [
        "src/katcha/editorial/source_intelligence/requirements.py",
        "src/katcha/services/editorial_media_preparation.py",
    ],
    "index": [
        "src/katcha/editorial/media_intake/scene_index.py",
        "src/katcha/editorial/media_intake/scene_worker.py",
    ],
    "matching": [
        "src/katcha/services/editorial_shot_matching.py",
        "src/katcha/services/editorial_semantic_matching.py",
    ],
    "render": ["src/katcha/editorial/visual_compiler.py", "src/katcha/editorial/render.py"],
    "publication": [
        "src/katcha/services/publications.py",
        "src/katcha/services/channel_learning.py",
    ],
}


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def migration_heads() -> list[str]:
    """Read additive source topology, never import migrations or inspect a live DB."""
    revisions, parents = set(), set()
    for path in sorted((ROOT / "migrations/versions").glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                names = [n.id for n in node.targets if isinstance(n, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names, value = [node.target.id], node.value
            else:
                continue
            if not set(names) & {"revision", "down_revision"}:
                continue
            literal = ast.literal_eval(value)
            if "revision" in names:
                revisions.add(literal)
            elif literal is not None:
                parents.update([literal] if isinstance(literal, str) else literal)
    return sorted(revisions - parents)


def build_report(capabilities: dict) -> dict:
    head = _git("rev-parse", "HEAD")
    changes = _git("status", "--porcelain", "--untracked-files=normal")
    owners = {
        key: {
            "source_paths": paths,
            "all_sources_present": all((ROOT / path).is_file() for path in paths),
            "live_status": "unknown",
        }
        for key, paths in OWNERS.items()
    }
    return {
        "schema_version": "AutonomousE2EPreflightV1",
        "source_sha": head,
        "working_tree_dirty": bool(changes) if changes is not None else None,
        "source_migration_heads": migration_heads(),
        "database_revision": None,
        "deployed_sha": None,
        "evidence_scope": "local source/configuration; browser health only if requested",
        "capabilities": capabilities,
        "owners": owners,
        "program_acceptance": "not_assessed",
        "blockers": [
            "Channel-specific auth, quotas, rights, storage, narration and review are unprobed.",
            "Current host release, workers and migrations require existing release diagnostics.",
            "A larger acquisition allowance does not expand measured scene-index capacity.",
            "Semantic retrieval and temporal verification require independent media evaluation.",
        ],
        "next_action": "Review docs/AUTONOMOUS_E2E_IMPLEMENTATION.md; use existing live "
        "acceptance runners only after their specific authorization gates are satisfied.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-browser",
        action="store_true",
        help="Probe the configured existing Chromium health endpoint only",
    )
    args = parser.parse_args()
    capabilities = (
        asyncio.run(retrieval_capabilities_checked())
        if args.probe_browser
        else retrieval_capabilities()
    )
    print(json.dumps(build_report(capabilities), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
