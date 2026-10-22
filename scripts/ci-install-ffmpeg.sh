#!/usr/bin/env bash
# Install FFmpeg only when absent; keep runner package-manager stalls bounded.
set -Eeuo pipefail
trap 'status=$?; printf "FFmpeg provisioning failed (exit %s) at %s\n" "$status" "$(date -u +%FT%TZ)" >&2' ERR

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  echo 'FFmpeg and ffprobe already available; skipping APT.'
else
  echo "Refreshing APT indexes at $(date -u +%FT%TZ)"
  sudo timeout --signal=TERM --kill-after=10s 180s \
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
  echo "Installing FFmpeg at $(date -u +%FT%TZ)"
  sudo env DEBIAN_FRONTEND=noninteractive timeout --signal=TERM --kill-after=10s 240s \
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
    -o Dpkg::Use-Pty=0 install -y --no-install-recommends ffmpeg
fi

command -v ffmpeg
command -v ffprobe
ffmpeg -version | sed -n '1p'
ffprobe -version | sed -n '1p'
echo "FFmpeg provisioning complete at $(date -u +%FT%TZ)"
