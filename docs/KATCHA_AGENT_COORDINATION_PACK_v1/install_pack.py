#!/usr/bin/env python3
"""Safely stage the Katcha coordination pack in an existing feature branch.

Usage: python install_pack.py --repo /path/to/katcha [--apply]
By default performs a dry run. Never creates Git branches, commits or PRs.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path

PACK = Path(__file__).resolve().parent
START = "## Coordinated Source Intelligence & Media Intake development"
FILES = (
    "README.md", "IMPLEMENTATION_PLAN.md", "CONTRACTS.md",
    "COORDINATION_PROTOCOL.md", "AGENT_BACKLOG.json",
    "AGENT_A.md", "AGENT_B.md", "CHECKSUMS.sha256",
    "AGENTS.md", "ROOT_AGENTS_MD_ADDITION.md",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--apply", action="store_true", help="make changes; default dry-run")
    args = p.parse_args()
    repo = args.repo.resolve(strict=True)
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise SystemExit("STOP: --repo must be Git repository root")
    branch = git(repo, "branch", "--show-current")
    if not branch or branch in {"main", "master"}:
        raise SystemExit("STOP: enter a non-main feature branch before staging pack")
    if git(repo, "status", "--porcelain"):
        raise SystemExit("STOP: working tree has changes; inspect/preserve before applying")
    if (repo / "src/katcha/editorial").is_dir() is False:
        raise SystemExit("STOP: this is not the expected Katcha repository")
    for line in (PACK / "CHECKSUMS.sha256").read_text().splitlines():
        sha, filename = line.split("  ", 1)
        if hashlib.sha256((PACK / filename).read_bytes()).hexdigest() != sha:
            raise SystemExit("STOP: pack checksum mismatch: " + filename)
    target = repo / "docs/agent-coordination"
    for name in FILES:
        dst = target / name
        if dst.exists() and dst.read_bytes() != (PACK / name).read_bytes():
            raise SystemExit("STOP: conflicting file already exists: " + str(dst))
    root_agents = repo / "AGENTS.md"
    if not root_agents.exists():
        raise SystemExit("STOP: repository AGENTS.md is missing")
    old = root_agents.read_text(encoding="utf-8")
    addition = (PACK / "ROOT_AGENTS_MD_ADDITION.md").read_text(encoding="utf-8")
    if START in old and addition.strip() not in old:
        raise SystemExit("STOP: root AGENTS.md already contains different coordination rules")
    if not old.endswith("\n"):
        raise SystemExit("STOP: root AGENTS.md unexpectedly lacks final newline")
    add = addition if addition.strip() not in old else ""
    to_add = [name for name in FILES if not (target / name).exists()]
    print("Repository:", repo)
    print("Branch:", branch)
    print("Will stage", len(to_add), "new coordination files")
    print("Root AGENTS.md:", "append additive guidance" if add else "already up to date")
    if not args.apply:
        print("DRY RUN only. Rerun with --apply to copy files on this feature branch.")
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in to_add:
        shutil.copyfile(PACK / name, target / name)
    if add:
        with root_agents.open("a", encoding="utf-8") as out:
            out.write(add)
    print("STAGED. Inspect git diff; run checks; commit and open reviewed PR.")


if __name__ == "__main__":
    main()
