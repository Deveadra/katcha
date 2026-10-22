#!/usr/bin/env bash
# GitHub Ubuntu runners put the Azure Ubuntu mirror first. If it stalls,
# package installs can consume an entire CI job without running tests.
set -Eeuo pipefail

if [[ -f /etc/apt/apt-mirrors.txt ]] && grep -q 'azure.archive.ubuntu.com' /etc/apt/apt-mirrors.txt; then
  mirror_list="$(mktemp)"
  trap 'rm -f "$mirror_list"' EXIT
  printf '%s\n' 'http://archive.ubuntu.com/ubuntu/' > "$mirror_list"
  grep -vFx 'http://archive.ubuntu.com/ubuntu/' /etc/apt/apt-mirrors.txt >> "$mirror_list" || true
  sudo install -m 0644 "$mirror_list" /etc/apt/apt-mirrors.txt
  echo 'Prioritized official Ubuntu archive; retained existing mirrors as fallbacks.'
else
  echo 'APT mirror list is not the expected Azure-first file; leaving mirrors unchanged.'
fi

printf 'Acquire::Retries "1";\nAcquire::http::Timeout "15";\nAcquire::https::Timeout "15";\n' \
  | sudo tee /etc/apt/apt.conf.d/99katcha-ci-network >/dev/null
echo 'Configured bounded APT network retries and timeouts.'
