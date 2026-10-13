# Temporary Windows-host repair for Codex Desktop's WSL helper links.
# Run from Windows PowerShell after Codex Desktop starts, before assigning agents.
# Does not change repositories, Codex configuration, or the WSL installation.
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$Distro = 'Ubuntu-24.04',
    [ValidateNotNullOrEmpty()]
    [string]$CodexRoot = (Join-Path $env:USERPROFILE '.codex'),
    [switch]$CheckOnly,
    [switch]$Diagnose
)

$ErrorActionPreference = 'Stop'

if ($CheckOnly -and $Diagnose) {
    throw 'Choose either -CheckOnly or -Diagnose, not both.'
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'wsl.exe is unavailable. Run this script on the Windows host.'
}

# Resolve the Windows profile path inside the selected WSL distribution.
$roots = @(& wsl.exe --distribution $Distro --exec wslpath -u $CodexRoot)
if ($LASTEXITCODE -ne 0 -or $roots.Count -ne 1 -or -not $roots[0].Trim()) {
    throw "Cannot map the Codex root into WSL ($Distro): $CodexRoot"
}
$linuxRoot = $roots[0].Trim()

# Pass a UTF-8/no-BOM, LF-only Bash file via the mounted Windows temp directory.
# Avoid PowerShell pipe encoding and CRLF corruption of bash -s input.
$tempScript = Join-Path $env:TEMP ('codex-wsl-preflight-{0}.sh' -f [guid]::NewGuid().ToString('N'))
$bash = @'
set -euo pipefail
root="$1"
mode="$2"

if [ ! -d "$root" ]; then
    echo "STOP: Codex root is missing in WSL: $root" >&2
    exit 1
fi

# Process /proc entries belong to the selected Linux user; never repair an
# unrelated user's process. The helper .lock is the strongest identity signal.
uid="$(id -u)"
all_appservers=0
codex_appservers=0
expected_layout=0
matches=()

for proc in /proc/[0-9]*; do
    [ "$(stat -c %u "$proc" 2>/dev/null || true)" = "$uid" ] || continue

    # Detect app-server first. Its executable can move between Codex releases.
    if ! grep -zFxq -- 'app-server' "$proc/cmdline" 2>/dev/null; then
        continue
    fi
    all_appservers=$((all_appservers + 1))

    pid="${proc##*/}"
    exe="$(readlink "$proc/exe" 2>/dev/null || true)"
    # Do not accept deleted executables, arbitrary names, or empty paths.
    [ "${exe##*/}" = codex ] || continue
    [ -x "$exe" ] || continue
    codex_appservers=$((codex_appservers + 1))
    case "$exe" in
        "$root"/bin/wsl/*/codex) expected_layout=$((expected_layout + 1)) ;;
    esac

    # Accept an alternate executable location ONLY when the running, same-user
    # Codex app-server actually has an FD on this installation's helper lock.
    for fd in "$proc"/fd/*; do
        target="$(readlink "$fd" 2>/dev/null || true)"
        case "$target" in
            "$root"/tmp/arg0/codex-arg0*/.lock)
                matches+=("$pid|$exe|${target%/.lock}")
                break
                ;;
        esac
    done
done

printf 'Discovery: app-server=%s codex-app-server=%s expected-bin-layout=%s valid-helper-locks=%s\n' \
    "$all_appservers" "$codex_appservers" "$expected_layout" "${#matches[@]}"

if [ "$mode" = diagnose ]; then
    if [ "${#matches[@]}" -eq 1 ]; then
        IFS='|' read -r dpid dexe dhelper <<< "${matches[0]}"
        printf 'MATCH: PID=%s executable=%s helper=%s\n' "$dpid" "$dexe" "$dhelper"
    elif [ "${#matches[@]}" -eq 0 ]; then
        echo 'DIAGNOSTIC: No verified live helper. No changes made.'
    else
        echo 'DIAGNOSTIC: Multiple verified helpers. No changes made.'
    fi
    echo 'CODEX_WSL_HELPER_DIAGNOSTIC_COMPLETE'
    exit 0
fi

if [ "${#matches[@]}" -ne 1 ]; then
    if [ "${#matches[@]}" -gt 1 ]; then
        echo 'STOP: Multiple verified Codex helpers. No links changed.' >&2
    elif [ "$all_appservers" -eq 0 ]; then
        echo 'STOP: No app-server process is visible in this WSL distribution.' >&2
        echo 'Check that Codex Desktop has opened the Ubuntu-24.04 WSL workspace; launching the app alone may not start its WSL backend.' >&2
    elif [ "$codex_appservers" -eq 0 ]; then
        echo 'STOP: app-server process found, but none has a live executable named codex.' >&2
    else
        echo 'STOP: Codex app-server found but it does not hold the expected live helper .lock under this Codex root.' >&2
        echo 'The helper may use a different root, may have exited, or may not be initialized yet.' >&2
    fi
    echo 'Use -Diagnose for a read-only discovery summary. No links changed.' >&2
    exit 1
fi
IFS='|' read -r pid exe helper <<< "${matches[0]}"

# Fail closed when process identity, ownership, or helper path has changed.
if [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" != "$exe" ] || \
   ! grep -zFxq -- 'app-server' "/proc/$pid/cmdline" 2>/dev/null; then
    echo 'STOP: Backend exited or changed during detection.' >&2
    exit 1
fi
if [ "$(dirname "$helper")" != "$root/tmp/arg0" ] || \
   [[ "$(basename "$helper")" != codex-arg0* ]] || \
   [ ! -d "$helper" ] || [ -L "$helper" ]; then
    echo "STOP: Helper directory is missing or unexpected: $helper" >&2
    exit 1
fi

owns_lock=0
for fd in "/proc/$pid"/fd/*; do
    if [ "$(readlink "$fd" 2>/dev/null || true)" = "$helper/.lock" ]; then
        owns_lock=1
        break
    fi
done
if [ "$owns_lock" -ne 1 ] || [ ! -x "$exe" ]; then
    echo 'STOP: Backend no longer holds this helper lock or its executable is unavailable.' >&2
    exit 1
fi

printf 'Backend PID: %s\nHelper: %s\n' "$pid" "$helper"
names=(codex-linux-sandbox codex-execve-wrapper apply_patch applypatch)
missing=0

# Examine every destination before changing any of them.
for name in "${names[@]}"; do
    path="$helper/$name"
    if [ -L "$path" ]; then
        if [ "$(readlink "$path")" != "$exe" ]; then
            echo "STOP: Unexpected existing symlink: $path" >&2
            exit 1
        fi
    elif [ -e "$path" ]; then
        echo "STOP: Existing non-symlink file: $path" >&2
        exit 1
    elif [ "$mode" = check ]; then
        echo "MISSING: $name" >&2
        missing=1
    fi
done

if [ "$missing" -ne 0 ]; then
    echo 'STOP: Helper requires repair. Run this script without -CheckOnly.' >&2
    exit 1
fi

# No writes in check mode. Repair adds only missing links to this live helper.
for name in "${names[@]}"; do
    path="$helper/$name"
    if [ "$mode" = repair ] && [ ! -L "$path" ]; then
        ln -s "$exe" "$path"
    fi
    if [ ! -x "$path" ]; then
        echo "STOP: Helper link is not executable: $path" >&2
        exit 1
    fi
    echo "PASS: $name"
done

if [ "$mode" = check ]; then
    echo 'CODEX_WSL_HELPER_CHECK_OK'
else
    echo 'CODEX_WSL_HELPER_REPAIR_OK'
fi
'@

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $lf = $bash.Replace("`r`n", "`n").Replace("`r", "`n")
    [System.IO.File]::WriteAllText($tempScript, $lf + "`n", $utf8)

    $scriptPaths = @(& wsl.exe --distribution $Distro --exec wslpath -u $tempScript)
    if ($LASTEXITCODE -ne 0 -or $scriptPaths.Count -ne 1 -or -not $scriptPaths[0].Trim()) {
        throw 'Cannot map the temporary Bash script into WSL.'
    }

    $mode = if ($Diagnose) { 'diagnose' } elseif ($CheckOnly) { 'check' } else { 'repair' }
    $linuxScript = $scriptPaths[0].Trim()
    $output = @(& wsl.exe --distribution $Distro --exec bash $linuxScript $linuxRoot $mode)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) { Write-Host $line }
    if ($exitCode -ne 0) {
        throw "Codex WSL helper $mode failed (exit $exitCode); see STOP/MISSING diagnostics above."
    }

    $marker = if ($Diagnose) { 'CODEX_WSL_HELPER_DIAGNOSTIC_COMPLETE' } elseif ($CheckOnly) { 'CODEX_WSL_HELPER_CHECK_OK' } else { 'CODEX_WSL_HELPER_REPAIR_OK' }
    if ($output -notcontains $marker) {
        throw "Codex helper reported exit 0 without $marker."
    }
    Write-Host "SUCCESS: Codex WSL helper $mode completed for $Distro."
}
finally {
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
}
