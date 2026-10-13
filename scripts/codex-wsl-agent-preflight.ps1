# Temporary Windows-host repair for Codex Desktop's WSL helper links.
# Run from Windows PowerShell after Codex Desktop starts, before assigning agents.
# Does not change repositories, Codex configuration, or the WSL installation.
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$Distro = 'Ubuntu-24.04',
    [ValidateNotNullOrEmpty()]
    [string]$CodexRoot = (Join-Path $env:USERPROFILE '.codex'),
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

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

matches=()
backends=0
for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    exe="$(readlink "$proc/exe" 2>/dev/null || true)"
    case "$exe" in
        "$root"/bin/wsl/*/codex) ;;
        *) continue ;;
    esac

    # Require the literal app-server argument, rather than a substring match.
    if ! grep -zFxq -- 'app-server' "$proc/cmdline" 2>/dev/null; then
        continue
    fi
    backends=$((backends + 1))

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

if [ "${#matches[@]}" -ne 1 ]; then
    echo "STOP: Expected one live Codex Desktop helper; found ${#matches[@]} (app-server processes: $backends)." >&2
    echo 'Start Codex Desktop and open its WSL workspace; close duplicate Desktop backends before retrying.' >&2
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

    $mode = if ($CheckOnly) { 'check' } else { 'repair' }
    $linuxScript = $scriptPaths[0].Trim()
    $output = @(& wsl.exe --distribution $Distro --exec bash $linuxScript $linuxRoot $mode)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) { Write-Host $line }
    if ($exitCode -ne 0) {
        throw "Codex WSL helper $mode failed (exit $exitCode); see STOP/MISSING diagnostics above."
    }

    $marker = if ($CheckOnly) { 'CODEX_WSL_HELPER_CHECK_OK' } else { 'CODEX_WSL_HELPER_REPAIR_OK' }
    if ($output -notcontains $marker) {
        throw "Codex helper reported exit 0 without $marker."
    }
    Write-Host "SUCCESS: Codex WSL helper $mode completed for $Distro."
}
finally {
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
}
