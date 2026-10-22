#!/usr/bin/env bash
# On GitHub Ubuntu runners, mirror+file may keep selecting a slow Azure mirror.
# Use the signed official Ubuntu archive only for this ephemeral CI runner.
set -Eeuo pipefail

changed=false
for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
  [[ -f "$source_file" ]] || continue
  if grep -Fq 'mirror+file:/etc/apt/apt-mirrors.txt' "$source_file" || \
     grep -Fq 'azure.archive.ubuntu.com/ubuntu' "$source_file"; then
    sudo sed -i \
      -e 's#mirror+file:/etc/apt/apt-mirrors\.txt#http://archive.ubuntu.com/ubuntu#g' \
      -e 's#http://azure\.archive\.ubuntu\.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' \
      "$source_file"
    echo "Pinned Ubuntu archive in $source_file"
    changed=true
  fi
done

if [[ "$changed" != true ]]; then
  echo 'No GitHub Azure mirror references found; retaining runner APT sources.'
fi
printf 'Acquire::Retries "1";\nAcquire::http::Timeout "15";\nAcquire::https::Timeout "15";\n' \
  | sudo tee /etc/apt/apt.conf.d/99katcha-ci-network >/dev/null
echo 'Configured bounded APT network retries and timeouts.'
