#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -n "$0" "$@"
fi

if [[ "$#" -lt 5 || "$#" -gt 6 ]]; then
    echo "Usage: $0 DEPLOYMENT_ID DEPLOYMENT_EPOCH RELEASE_SHA COST_CLASS RECOVERY_INCIDENT_ID [PAID_EXPIRES_AT]" >&2
    exit 2
fi

deployment_id="$1"
deployment_epoch="$2"
release_sha="$3"
cost_class="$4"
incident_id="$5"
paid_expires_at="${6:-}"

root="${KATCHA_REPO_ROOT:-/opt/katcha}"
env_file="${KATCHA_ENV_FILE:-/etc/katcha/katcha.env}"

if ! mountpoint -q /srv/katcha; then
    mount /srv/katcha
fi
if ! mountpoint -q /srv/katcha; then
    echo "Durable Katcha volume is not mounted at /srv/katcha" >&2
    exit 20
fi
for directory in postgres handoff recovery backups-local; do
    if [[ ! -d "/srv/katcha/${directory}" ]]; then
        echo "Missing durable directory /srv/katcha/${directory}" >&2
        exit 21
    fi
done

cd "${root}"
git fetch --no-tags origin "${release_sha}"
git checkout --detach "${release_sha}"
test "$(git rev-parse HEAD)" = "${release_sha}"

identity_args=(
    --env-file "${env_file}"
    --deployment-id "${deployment_id}"
    --deployment-epoch "${deployment_epoch}"
    --release-sha "${release_sha}"
    --cost-class "${cost_class}"
    --recovery-incident-id "${incident_id}"
)
if [[ -n "${paid_expires_at}" ]]; then
    identity_args+=(--paid-expires-at "${paid_expires_at}")
fi
python3 scripts/apply_recovery_identity.py "${identity_args[@]}"
python3 scripts/validate_production_runtime.py --env-file "${env_file}"

systemctl daemon-reload
systemctl restart katcha.service

for attempt in $(seq 1 90); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/v1/health/ready >/dev/null; then
        systemctl is-active --quiet katcha.service
        echo "Katcha recovery candidate is locally ready: ${deployment_id}@${deployment_epoch}"
        exit 0
    fi
    if ! systemctl is-active --quiet katcha.service; then
        journalctl -u katcha.service --no-pager -n 120 >&2 || true
        exit 30
    fi
    sleep 2
done

journalctl -u katcha.service --no-pager -n 120 >&2 || true
echo "Katcha candidate did not become ready before timeout" >&2
exit 31
