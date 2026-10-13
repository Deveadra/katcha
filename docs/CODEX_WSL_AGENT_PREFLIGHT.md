# Codex Desktop WSL agent preflight (temporary Windows workaround)

This is a **Windows-host workaround** for a Codex Desktop WSL-helper startup bug. It is not a Katcha runtime dependency, a Linux environment installer, or a guaranteed fix for every `CreateProcess` failure. Do not schedule it as a persistent background service.

## Before giving WSL agents work

1. Start **Codex Desktop** and select/open the `Ubuntu-24.04` WSL workspace so its `app-server` backend is running.
2. On the **Windows host**, run the repository-root `Codex-Agents-Preflight.cmd` (double-click in File Explorer), or use this PowerShell command for the standard checkout:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu-24.04\home\tycarroll\src\katcha\scripts\codex-wsl-agent-preflight.ps1"
   ```

   Change the checkout path only if yours differs. Alternatively, from a Windows PowerShell prompt where the checkout is the working directory, run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-wsl-agent-preflight.ps1`.
3. Confirm that each of `codex-linux-sandbox`, `codex-execve-wrapper`, `apply_patch`, and `applypatch` reports `PASS`, followed by **`CODEX_WSL_HELPER_REPAIR_OK`** and a `SUCCESS` line.
4. Start/retry the Codex WSL agents. Have them verify `pwd`, `git status --short --branch`, and the intended workspace before making edits. Successful preflight confirms *helper links*, not that Codex Desktop can create a shell inside its sandbox.

Repeat after **restarting Codex Desktop** if it creates a new temporary helper. A Windows/WSL reboot is not required just to re-run this script. Re-running it against the **same** live helper is safe and idempotent; it creates only missing links.

## Commands and options

From Windows PowerShell:

```powershell
# Repair the live helper (default)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-wsl-agent-preflight.ps1

# Non-mutating validation (fails if any required link is absent)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-wsl-agent-preflight.ps1 -CheckOnly

# Non-default WSL distro / Codex installation
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-wsl-agent-preflight.ps1 -Distro Ubuntu-24.04 -CodexRoot 'C:\Users\Me\.codex'
```

`Codex-Agents-Preflight.cmd` also forwards optional arguments such as `-CheckOnly`. Scripts are versioned in the repository; do **not** copy them into Codex's temporary helper directory.

## What it does and does not do

The PowerShell script converts the Windows Codex root to a Linux path using `wslpath`, places an LF-only, UTF-8/no-BOM Bash script in a uniquely named temporary Windows file, and invokes that script in the requested WSL distribution. It identifies **exactly one** running Codex `app-server` under the expected `.codex/bin/wsl/.../codex` path that still holds a `.codex/tmp/arg0/codex-arg0*/.lock` descriptor. It checks for unexpected paths/files, then adds only missing symlinks pointing to that active executable. It removes its own temporary Bash file in a `finally` block.

It does **not** elevate privileges, edit Codex application binaries/configuration, make permanent mounts, modify repositories/Git, kill processes, install services, or replace unexpected links/files. It refuses ambiguous/multiple helpers, a lost lock, an absent helper directory, a changed backend, and inaccessible/existing unexpected paths. On `STOP`, read the specific message; fix the stated issue instead of blindly looping the script or deleting Codex files.

**Recovery guidance:** If `STOP: Expected one live ...` appears, first verify Codex Desktop's WSL workspace is open and that duplicate Desktop backends are not running. If `ln -s` fails with a permissions/DriveFS error, stop: this script will not bypass filesystem restrictions. If the Codex agent still reports `CreateProcess: No such file or directory` after a successful helper repair, the shell-launch defect remains; collect the agent-side failure for the existing OpenAI support report. A WSL shell run by Windows PowerShell does not prove Codex Desktop's sandbox can spawn its own shell.

This workaround should be removed or retired once the upstream issue is fixed and verified, rather than made a permanent start dependency.
