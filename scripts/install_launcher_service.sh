#!/usr/bin/env bash
# Run once on the machine that hosts Katcha, as its normal user.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=$(command -v python3)
command -v systemctl >/dev/null || { echo 'systemd is required.' >&2; exit 1; }
# Quote systemd specifiers and paths, including checkout paths containing spaces.
quote_unit() { local value=${1//\\/\\\\}; value=${value//\"/\\\"}; value=${value//%/%%}; printf '"%s"' "$value"; }
mkdir -p "$HOME/.config/systemd/user"
unit="$HOME/.config/systemd/user/katcha-launcher.service"
if [[ -e "$unit" ]]; then cp -p -- "$unit" "$unit.backup.$(date +%s)"; fi
{
    echo '[Unit]'
    echo 'Description=Katcha background supervisor'
    echo '[Service]'
    printf 'WorkingDirectory=%s\n' "$(quote_unit "$root")"
    printf 'ExecStart=%s %s --no-browser\n' "$(quote_unit "$python")" "$(quote_unit "$root/launcher/runtime.py")"
    echo 'Restart=always'
    echo 'RestartSec=10'
    echo 'UMask=0077'
    echo '[Install]'
    echo 'WantedBy=default.target'
} > "$unit"
systemctl --user daemon-reload
systemctl --user enable --now katcha-launcher.service
echo 'Open http://localhost:8765 and click Start once to enable automatic recovery.'
echo 'For operation after logout, an administrator can enable loginctl enable-linger for your user.'
echo 'WSL and Docker Desktop must remain running; this does not start Windows or wake a sleeping PC.'
