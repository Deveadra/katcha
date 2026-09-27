# Windows entrypoint for Windows checkouts and \\wsl.localhost\Distro\... checkouts.
param([switch]$InstallShortcut)
$ErrorActionPreference = 'Stop'
$launcherPath = Join-Path $PSScriptRoot 'Katcha.ps1'
if ($InstallShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Katcha.lnk'))
    $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -File "' + $launcherPath + '"'
    $shortcut.Description = 'Open the Katcha workspace and runtime console'
    $shortcut.Save()
    Write-Host 'Katcha desktop shortcut created.'
    exit 0
}
try {
    $wslArguments = @()
    if ($PSScriptRoot -match '^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)(\\.*)?$') {
        $distribution = $Matches[1]
        $linuxPath = $Matches[2] -replace '\\', '/'
        if (-not $linuxPath) { $linuxPath = '/' }
        $wslArguments += @('--distribution', $distribution, '--cd', $linuxPath)
    } else {
        $wslArguments += @('--cd', $PSScriptRoot)
    }
    $wslArguments += @('--exec', 'bash', './Katcha.sh')
    & wsl.exe @wslArguments
    if ($LASTEXITCODE -ne 0) { throw "Katcha exited with code $LASTEXITCODE. Check Docker Desktop and WSL integration." }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
