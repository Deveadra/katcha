# Installing this pack into the Katcha repository

**Already installed:** the authoritative pack is tracked at `docs/agent-coordination/`.
Do not run this installer against the existing Katcha checkout. The instructions
below are retained only for installing the pack into a repository without it.

**Do not extract on top of `main` or overwrite existing `AGENTS.md`.** This pack
provides a guarded installer; it does not create branches, commit, deploy,
push, or merge on your behalf.

1. Unzip `KATCHA_AGENT_COORDINATION_PACK_v1.zip` somewhere outside the repository.
2. Confirm the current Katcha repository and Git status. Make a new, isolated
   feature branch from accepted `main` with a clean worktree, without resetting
   or discarding any existing changes.
3. From a Python 3.12+ shell run:

   ```powershell
   python .\katcha-agent-coordination\install_pack.py --repo C:\src\katcha
   ```

   The command above **only previews** staged changes; use your actual extraction
   path if different. For WSL, substitute `python3` and `~/src/katcha`.

4. Once the preview is correct, run with `--apply` to stage new files:

   ```powershell
   python .\katcha-agent-coordination\install_pack.py --repo C:\src\katcha --apply
   ```

5. Inspect `git status`, `git diff` (including untracked files), run applicable
   checks, commit on that feature branch and open a PR. Do not merge without
   explicit approval and CI evidence. The installer preserves existing root
   `AGENTS.md` and appends only the new coordination guidance.

**Safety:** If there are uncommitted changes, a conflicting destination file,
main branch selection, missing repo structure or checksum mismatches, installation
stops without modifying files. This is a local staging helper, not a GitHub
write integration; it does not claim that the pack is already committed.
