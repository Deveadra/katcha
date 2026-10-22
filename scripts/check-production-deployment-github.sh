#!/usr/bin/env bash
# Read-only operator check before requesting a protected GitHub production deploy.
# Never reads or prints deployment signing key material.
set -euo pipefail

repo="Deveadra/katcha"
environment="katcha-production"
gate_name="KATCHA_PRODUCTION_DEPLOY_ENABLED"
secret_name="KATCHA_PRODUCTION_DEPLOY_HMAC_KEY"

if [[ $# -ne 0 ]]; then
  echo "Usage: bash scripts/check-production-deployment-github.sh" >&2
  exit 2
fi
if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  echo "STOP: Authenticate GitHub CLI on WSL with gh auth login." >&2
  exit 2
fi

if ! value="$(gh variable get "$gate_name" --repo "$repo" 2>/dev/null)"; then
  echo "STOP: Repository Actions variable $gate_name is absent or inaccessible." >&2
  exit 2
fi
if [[ "$value" != "true" ]]; then
  echo "STOP: Repository Actions variable $gate_name must remain true through approval." >&2
  exit 2
fi

if ! env_duplicates="$(gh variable list --repo "$repo" --env "$environment" \
    --json name --jq ".[] | select(.name == \"$gate_name\") | .name")"; then
  echo "STOP: Could not inspect GitHub Environment variables." >&2
  exit 2
fi
if [[ -n "$env_duplicates" ]]; then
  echo "STOP: Conflicting $environment Environment variable $gate_name exists." >&2
  echo "Remove the duplicate variable; keep the repository Actions variable authoritative." >&2
  exit 2
fi

if ! matching_secret="$(gh secret list --repo "$repo" --env "$environment" \
    --json name --jq ".[] | select(.name == \"$secret_name\") | .name")"; then
  echo "STOP: Could not inspect protected Environment secret metadata." >&2
  exit 2
fi
if [[ "$matching_secret" != "$secret_name" ]]; then
  echo "STOP: Missing Environment secret $secret_name in $environment." >&2
  echo "Restore the existing matching OCI signing key; do not regenerate blindly." >&2
  exit 2
fi

echo "GITHUB_DEPLOY_AUTH_SCOPE_OK repo_gate=true duplicate_environment_gate=absent environment_signing_secret=present"
echo "Signing secret content and OCI key equivalence cannot be verified by GitHub metadata."
