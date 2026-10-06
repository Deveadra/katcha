#!/usr/bin/env bash
set -euo pipefail

REPO="${KATCHA_GITHUB_REPO:-Deveadra/katcha}"
APPLY=false
if [[ "${1:-}" == "--apply" ]]; then
    APPLY=true
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--apply]" >&2
    exit 2
fi

COMPARTMENT_ID="${KATCHA_OCI_COMPARTMENT_ID:-}"
VCN_ID="${KATCHA_OCI_VCN_ID:-ocid1.vcn.oc1.iad.amaaaaaaoa2nvwqaplz3s4px2rcgjxfmkuvcixwmxay5uj243i6qidtz24ta}"
ROUTE_TABLE_ID="${KATCHA_OCI_PRIVATE_ROUTE_TABLE_ID:-ocid1.routetable.oc1.iad.aaaaaaaah2mbbpikl2pqy4pzjckhxyrxxddoqvxbztdtofp35yysanb6aefa}"
OCI_REGION="${OCI_REGION:-us-ashburn-1}"
OCI_KEY_FILE="${OCI_API_PRIVATE_KEY_FILE:-$HOME/.config/katcha/oci-github-recovery/oci_api_key.pem}"
OCI_CONFIG_FILE="${OCI_CONFIG_FILE:-$HOME/.config/katcha/oci-github-recovery/config}"
SSH_PUBLIC_KEY_FILE="${KATCHA_OCI_SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/katcha-oci.pub}"
SSH_PRIVATE_KEY_FILE="${KATCHA_OCI_SSH_PRIVATE_KEY_FILE:-$HOME/.ssh/katcha-oci}"
BASTION_NAME="${KATCHA_OCI_BASTION_NAME:-katchaprod}"
ADMIN_NSG_NAME="${KATCHA_OCI_ADMIN_NSG_NAME:-katcha-prod-admin-nsg}"
SERVICE_GATEWAY_NAME="${KATCHA_OCI_SERVICE_GATEWAY_NAME:-katcha-prod-services}"
LOCAL_PORT="${KATCHA_OCI_BASTION_LOCAL_PORT:-22022}"
TARGET_USER="${KATCHA_OCI_TARGET_USER:-ubuntu}"
DEVICE_PATH="${KATCHA_OCI_DATA_VOLUME_DEVICE_PATH:-/dev/oracleoci/oraclevdb}"

log() {
    printf '[katcha-oci-access] %s\n' "$1"
}

fail() {
    printf '[katcha-oci-access] ERROR: %s\n' "$1" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

for command in gh curl jq python3 ssh git; do
    require_command "$command"
done

gh auth status --hostname github.com >/dev/null 2>&1 ||
    fail "GitHub CLI is not authenticated"

github_variables_json="$(
    gh api "repos/$REPO/actions/variables?per_page=100"
)" || fail "could not read GitHub Actions variables"

gh_var() {
    local name="$1"
    jq -r --arg name "$name"         '.variables[]? | select(.name == $name) | .value'         <<<"$github_variables_json" | head -n 1
}

acquired="$(gh_var KATCHA_OCI_BOOTSTRAP_ACQUIRED)"
poll_enabled="$(gh_var KATCHA_OCI_BOOTSTRAP_POLL_ENABLED)"
instance_id="$(gh_var KATCHA_OCI_PRIMARY_INSTANCE_ID)"
availability_domain="$(gh_var KATCHA_OCI_AVAILABILITY_DOMAIN)"
subnet_id="$(gh_var KATCHA_OCI_SUBNET_ID)"
volume_id="$(gh_var KATCHA_OCI_DATA_VOLUME_ID)"

[[ "$acquired" == "true" ]] ||
    fail "GitHub does not record a completed OCI A1 acquisition"
[[ "$poll_enabled" != "true" ]] ||
    fail "capacity polling is still enabled; refusing to provision while acquisition is active"
[[ "$instance_id" == ocid1.instance.* ]] ||
    fail "KATCHA_OCI_PRIMARY_INSTANCE_ID is missing or invalid"
[[ "$subnet_id" == ocid1.subnet.* ]] ||
    fail "KATCHA_OCI_SUBNET_ID is missing or invalid"
[[ "$volume_id" == ocid1.volume.* ]] ||
    fail "KATCHA_OCI_DATA_VOLUME_ID is missing or invalid"

if [[ -z "$COMPARTMENT_ID" ]]; then
    COMPARTMENT_ID="$(gh_var KATCHA_OCI_COMPARTMENT_ID)"
fi
[[ "$COMPARTMENT_ID" == ocid1.compartment.* ]] ||
    fail "KATCHA_OCI_COMPARTMENT_ID is missing or invalid"

[[ -s "$OCI_KEY_FILE" ]] || fail "missing OCI API key: $OCI_KEY_FILE"
[[ -s "$SSH_PUBLIC_KEY_FILE" ]] || fail "missing SSH public key: $SSH_PUBLIC_KEY_FILE"
[[ -s "$SSH_PRIVATE_KEY_FILE" ]] || fail "missing SSH private key: $SSH_PRIVATE_KEY_FILE"
chmod 0600 "$SSH_PRIVATE_KEY_FILE"

mkdir -p "$(dirname "$OCI_CONFIG_FILE")"
chmod 0700 "$(dirname "$OCI_CONFIG_FILE")"

if [[ ! -s "$OCI_CONFIG_FILE" ]]; then
    tenancy="${OCI_TENANCY_OCID:-}"
    user="${OCI_USER_OCID:-}"
    fingerprint="${OCI_FINGERPRINT:-}"

    [[ -n "$tenancy" ]] || read -r -p "OCI tenancy OCID: " tenancy
    [[ -n "$user" ]] || read -r -p "Dedicated OCI recovery user OCID: " user
    [[ -n "$fingerprint" ]] || read -r -p "OCI API key fingerprint: " fingerprint

    [[ "$tenancy" == ocid1.tenancy.* ]] || fail "invalid tenancy OCID"
    [[ "$user" == ocid1.user.* ]] || fail "invalid OCI user OCID"

    umask 077
    cat > "$OCI_CONFIG_FILE" <<EOF
[DEFAULT]
user=$user
fingerprint=$fingerprint
tenancy=$tenancy
region=$OCI_REGION
key_file=$OCI_KEY_FILE
EOF
    chmod 0600 "$OCI_CONFIG_FILE"
    log "Wrote local OCI CLI config at $OCI_CONFIG_FILE"
fi

OCI_VENV="${KATCHA_OCI_CLI_VENV:-$HOME/.cache/katcha/oci-cli-3.94.1}"
OCI_BIN="$OCI_VENV/bin/oci"
if [[ ! -x "$OCI_BIN" ]]; then
    log "Installing isolated OCI CLI 3.94.1"
    python3 -m venv "$OCI_VENV"
    "$OCI_VENV/bin/python" -m pip install --quiet --upgrade pip
    "$OCI_VENV/bin/python" -m pip install --quiet 'oci-cli==3.94.1'
fi

oci_cmd() {
    SUPPRESS_LABEL_WARNING=True \
        "$OCI_BIN" --config-file "$OCI_CONFIG_FILE" "$@"
}

log "Verifying the acquired host before any network mutation"
subnet_json="$(oci_cmd network subnet get --subnet-id "$subnet_id")"
actual_vcn_id="$(jq -er '.data."vcn-id"' <<<"$subnet_json")"
actual_route_table_id="$(jq -er '.data."route-table-id"' <<<"$subnet_json")"
[[ "$actual_vcn_id" == "$VCN_ID" ]] ||
    fail "winning subnet is not attached to the expected production VCN"
[[ "$actual_route_table_id" == "$ROUTE_TABLE_ID" ]] ||
    fail "winning subnet is not using the expected private route table"

instance_json="$(oci_cmd compute instance get --instance-id "$instance_id")"
instance_state="$(jq -r '.data."lifecycle-state"' <<<"$instance_json")"
instance_shape="$(jq -r '.data.shape' <<<"$instance_json")"
instance_ad="$(jq -r '.data."availability-domain"' <<<"$instance_json")"
instance_ocpus="$(jq -r '.data."shape-config".ocpus' <<<"$instance_json")"
instance_memory="$(jq -r '.data."shape-config"."memory-in-gbs"' <<<"$instance_json")"

[[ "$instance_state" == "RUNNING" ]] || fail "acquired instance is not RUNNING: $instance_state"
[[ "$instance_shape" == "VM.Standard.A1.Flex" ]] || fail "acquired instance has wrong shape: $instance_shape"
[[ "$instance_ocpus" == "2" || "$instance_ocpus" == "2.0" ]] ||
    fail "acquired instance is not 2 OCPU"
[[ "$instance_memory" == "12" || "$instance_memory" == "12.0" ]] ||
    fail "acquired instance is not 12 GB"
[[ "$instance_ad" == "$availability_domain" ]] ||
    fail "GitHub primary AD does not match the acquired instance"

vnic_attachments="$(
    oci_cmd compute vnic-attachment list \
        --compartment-id "$COMPARTMENT_ID" \
        --instance-id "$instance_id" \
        --all
)"
vnic_id="$(
    jq -er '
      [.data[] | select(."lifecycle-state" != "DETACHED" and ."lifecycle-state" != "DETACHING")]
      | if length == 1 then .[0]."vnic-id" else error("expected one active VNIC") end
    ' <<<"$vnic_attachments"
)"
vnic_json="$(oci_cmd network vnic get --vnic-id "$vnic_id")"
private_ip="$(jq -er '.data."private-ip"' <<<"$vnic_json")"
public_ip="$(jq -r '.data."public-ip" // empty' <<<"$vnic_json")"
actual_subnet="$(jq -er '.data."subnet-id"' <<<"$vnic_json")"

[[ -z "$public_ip" ]] || fail "acquired instance unexpectedly has public IP $public_ip"
[[ "$actual_subnet" == "$subnet_id" ]] || fail "acquired instance is in the wrong subnet"

volume_attachments="$(
    oci_cmd compute volume-attachment list \
        --compartment-id "$COMPARTMENT_ID" \
        --volume-id "$volume_id" \
        --all
)"
attached_instance="$(
    jq -er '
      [.data[] | select(."lifecycle-state" == "ATTACHED")]
      | if length == 1 then .[0]."instance-id" else error("expected one ATTACHED volume attachment") end
    ' <<<"$volume_attachments"
)"
[[ "$attached_instance" == "$instance_id" ]] ||
    fail "durable volume is not attached to the acquired primary"

log "Verified: RUNNING 2 OCPU / 12 GB, private-only VNIC, durable volume ATTACHED"
log "Primary placement: $availability_domain / $private_ip"

operator_ip="${KATCHA_OPERATOR_PUBLIC_IP:-}"
if [[ -z "$operator_ip" ]]; then
    operator_ip="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
fi
if ! python3 - "$operator_ip" <<'PY'
import ipaddress
import sys
try:
    value = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if value.version == 4 else 1)
PY
then
    fail "could not determine a valid operator IPv4 address; set KATCHA_OPERATOR_PUBLIC_IP"
fi
operator_cidr="$operator_ip/32"

services_json="$(oci_cmd network service list --all)"
service_count="$(
    jq '[.data[] | select((.name // "") | test("^All .* Services In Oracle Services Network$"))] | length' <<<"$services_json"
)"
[[ "$service_count" == "1" ]] ||
    fail "expected exactly one regional 'All ... Services In Oracle Services Network' service"
service_id="$(
    jq -er '.data[] | select((.name // "") | test("^All .* Services In Oracle Services Network$")) | .id' <<<"$services_json"
)"
service_cidr="$(
    jq -er --arg id "$service_id" '.data[] | select(.id == $id) | ."cidr-block"' <<<"$services_json"
)"

gateway_list="$(
    oci_cmd network service-gateway list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --all
)"
gateway_id="$(
    jq -r --arg name "$SERVICE_GATEWAY_NAME" '
      [.data[] | select(."display-name" == $name and ."lifecycle-state" != "TERMINATED")]
      | if length == 1 then .[0].id elif length == 0 then "" else error("multiple matching service gateways") end
    ' <<<"$gateway_list"
)"

if [[ -z "$gateway_id" ]]; then
    if [[ "$APPLY" != "true" ]]; then
        log "PLAN: create service gateway '$SERVICE_GATEWAY_NAME' and route $service_cidr through it"
    else
        log "Creating OCI service gateway required by Bastion"
        services_payload="$(jq -cn --arg id "$service_id" '[{serviceId:$id}]')"
        gateway_id="$(
            oci_cmd network service-gateway create \
                --compartment-id "$COMPARTMENT_ID" \
                --vcn-id "$VCN_ID" \
                --display-name "$SERVICE_GATEWAY_NAME" \
                --services "$services_payload" \
                --wait-for-state AVAILABLE \
                --query 'data.id' \
                --raw-output
        )"
    fi
fi

if [[ -n "$gateway_id" ]]; then
    route_json="$(oci_cmd network route-table get --rt-id "$ROUTE_TABLE_ID")"
    has_service_route="$(
        jq -r --arg gateway "$gateway_id" --arg cidr "$service_cidr" '
          any(.data."route-rules"[]?;
            ."network-entity-id" == $gateway
            and .destination == $cidr
            and ."destination-type" == "SERVICE_CIDR_BLOCK"
          )
        ' <<<"$route_json"
    )"
    if [[ "$has_service_route" != "true" ]]; then
        if [[ "$APPLY" != "true" ]]; then
            log "PLAN: append the service-gateway route without replacing the existing NAT route"
        else
            log "Adding service-gateway route while preserving existing routes"
            route_rules="$(
                ROUTE_JSON="$route_json" python3 - "$gateway_id" "$service_cidr" <<'PY'
import json
import os
import sys
payload = json.loads(os.environ["ROUTE_JSON"])
gateway_id, service_cidr = sys.argv[1], sys.argv[2]
rules = []
for row in payload["data"].get("route-rules", []):
    item = {
        "destination": row.get("destination"),
        "destinationType": row.get("destination-type"),
        "networkEntityId": row.get("network-entity-id"),
    }
    if row.get("description"):
        item["description"] = row["description"]
    if row.get("route-type"):
        item["routeType"] = row["route-type"]
    rules.append(item)
rules.append({
    "destination": service_cidr,
    "destinationType": "SERVICE_CIDR_BLOCK",
    "networkEntityId": gateway_id,
    "routeType": "STATIC",
    "description": "Katcha private access to Oracle Services Network",
})
print(json.dumps(rules, separators=(",", ":")))
PY
            )"
            oci_cmd network route-table update \
                --rt-id "$ROUTE_TABLE_ID" \
                --route-rules "$route_rules" \
                --force >/dev/null
        fi
    fi
fi

bastion_list_err="$(mktemp)"
if bastion_list="$(
    oci_cmd bastion bastion list \
        --compartment-id "$COMPARTMENT_ID" \
        --all 2>"$bastion_list_err"
)"; then
    # OCI CLI 3.94.1 may emit an empty successful response when a compartment
    # contains no Bastions. Treat exit status, not stdout length, as success.
    if [[ -z "$bastion_list" ]]; then
        bastion_list='{"data":[]}'
    fi
else
    if grep -qiE 'notauthorizedornotfound|not authorized|notallowed|authorization' "$bastion_list_err"; then
        cat >&2 <<'EOF'
The dedicated OCI recovery identity does not yet have Bastion permission.
Add these temporary initial-bootstrap policy statements to the existing
root-tenancy Katcha policy, then rerun:

Allow group katcha-github-recovery to manage bastion-family in compartment katcha-prod
Allow group katcha-github-recovery to manage virtual-network-family in compartment katcha-prod
Allow group katcha-github-recovery to inspect work-requests in tenancy

After this foundation bootstrap succeeds, downgrade virtual-network-family back
to the normal recovery permission documented in the runbook:
Allow group katcha-github-recovery to use virtual-network-family in compartment katcha-prod
EOF
        rm -f "$bastion_list_err"
        exit 21
    fi
    cat "$bastion_list_err" >&2
    rm -f "$bastion_list_err"
    fail "could not list OCI bastions"
fi
rm -f "$bastion_list_err"

bastion_id="$(
    jq -r --arg name "$BASTION_NAME" '
      [.data[] | select(.name == $name and ."lifecycle-state" != "DELETED")]
      | if length == 1 then .[0].id elif length == 0 then "" else error("multiple matching bastions") end
    ' <<<"$bastion_list"
)"

if [[ -z "$bastion_id" ]]; then
    if [[ "$APPLY" != "true" ]]; then
        log "PLAN: create free OCI Bastion '$BASTION_NAME' in the active private subnet"
        log "PLAN: restrict Bastion clients to $operator_cidr"
    else
        [[ -n "$gateway_id" ]] || fail "service gateway must exist before Bastion creation"
        log "Creating OCI Bastion with operator allowlist $operator_cidr"
        bastion_id="$(
            oci_cmd bastion bastion create \
                --bastion-type standard \
                --compartment-id "$COMPARTMENT_ID" \
                --target-subnet-id "$subnet_id" \
                --name "$BASTION_NAME" \
                --client-cidr-list "[\"$operator_cidr\"]" \
                --max-session-ttl 10800 \
                --wait-for-state SUCCEEDED \
                --query 'data.id' \
                --raw-output
        )"
    fi
elif [[ "$APPLY" == "true" ]]; then
    log "Updating Bastion client allowlist to the current operator IP"
    oci_cmd bastion bastion update \
        --bastion-id "$bastion_id" \
        --client-cidr-list "[\"$operator_cidr\"]" \
        --force >/dev/null
fi

if [[ "$APPLY" != "true" ]]; then
    log "CHECK complete. No OCI network resources were changed."
    log "Run '$0 --apply' after Bastion IAM permission is present."
    exit 0
fi

bastion_ip=""
for _ in $(seq 1 60); do
    bastion_json="$(oci_cmd bastion bastion get --bastion-id "$bastion_id")"
    bastion_state="$(jq -r '.data."lifecycle-state"' <<<"$bastion_json")"
    bastion_ip="$(jq -r '.data."private-endpoint-ip-address" // empty' <<<"$bastion_json")"
    if [[ "$bastion_state" == "ACTIVE" && -n "$bastion_ip" ]]; then
        break
    fi
    sleep 5
done
[[ -n "$bastion_ip" ]] || fail "Bastion did not become ACTIVE with a private endpoint"

nsg_list="$(
    oci_cmd network nsg list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --all
)"
admin_nsg_id="$(
    jq -r --arg name "$ADMIN_NSG_NAME" '
      [.data[] | select(."display-name" == $name)]
      | if length == 1 then .[0].id elif length == 0 then "" else error("multiple matching admin NSGs") end
    ' <<<"$nsg_list"
)"
if [[ -z "$admin_nsg_id" ]]; then
    log "Creating target-specific admin NSG"
    admin_nsg_id="$(
        oci_cmd network nsg create \
            --compartment-id "$COMPARTMENT_ID" \
            --vcn-id "$VCN_ID" \
            --display-name "$ADMIN_NSG_NAME" \
            --query 'data.id' \
            --raw-output
    )"
fi

rules_json="$(oci_cmd network nsg rules list --nsg-id "$admin_nsg_id" --all)"
stale_rule_ids="$(
    jq -c --arg source "$bastion_ip/32" '[
      .data[]
      | select(
          .description == "OCI Bastion SSH to Katcha primary"
          and .source != $source
        )
      | .id
    ]' <<<"$rules_json"
)"
if [[ "$(jq 'length' <<<"$stale_rule_ids")" -gt 0 ]]; then
    log "Removing stale Bastion SSH rule(s) from the admin NSG"
    oci_cmd network nsg rules remove \
        --nsg-id "$admin_nsg_id" \
        --security-rule-ids "$stale_rule_ids" >/dev/null
    rules_json="$(oci_cmd network nsg rules list --nsg-id "$admin_nsg_id" --all)"
fi

has_ssh_rule="$(
    jq -r --arg source "$bastion_ip/32" '
      any(.data[]?;
        .direction == "INGRESS"
        and .protocol == "6"
        and .source == $source
        and (."tcp-options"."destination-port-range".min // 0) == 22
        and (."tcp-options"."destination-port-range".max // 0) == 22
      )
    ' <<<"$rules_json"
)"
if [[ "$has_ssh_rule" != "true" ]]; then
    log "Allowing SSH only from the Bastion private endpoint"
    security_rules="$(
        jq -cn --arg source "$bastion_ip/32" '[
          {
            direction:"INGRESS",
            protocol:"6",
            source:$source,
            sourceType:"CIDR_BLOCK",
            isStateless:false,
            description:"OCI Bastion SSH to Katcha primary",
            tcpOptions:{destinationPortRange:{min:22,max:22}}
          }
        ]'
    )"
    oci_cmd network nsg rules add \
        --nsg-id "$admin_nsg_id" \
        --security-rules "$security_rules" >/dev/null
fi

current_nsgs="$(jq -c '.data."nsg-ids" // []' <<<"$vnic_json")"
desired_nsgs="$(
    jq -cn --argjson current "$current_nsgs" --arg id "$admin_nsg_id" \
        '$current + [$id] | unique'
)"
if [[ "$desired_nsgs" != "$current_nsgs" ]]; then
    log "Attaching the admin NSG to the primary VNIC"
    oci_cmd network vnic update \
        --vnic-id "$vnic_id" \
        --nsg-ids "$desired_nsgs" \
        --force >/dev/null
fi

log "Creating a three-hour SSH port-forwarding Bastion session"
session_id="$(
    oci_cmd bastion session create-port-forwarding \
        --bastion-id "$bastion_id" \
        --display-name "katcha-bootstrap" \
        --ssh-public-key-file "$SSH_PUBLIC_KEY_FILE" \
        --target-private-ip "$private_ip" \
        --target-port 22 \
        --session-ttl 10800 \
        --wait-for-state SUCCEEDED \
        --query 'data.id' \
        --raw-output
)"

for _ in $(seq 1 60); do
    session_state="$(
        oci_cmd bastion session get --session-id "$session_id" \
            --query 'data."lifecycle-state"' --raw-output
    )"
    [[ "$session_state" == "ACTIVE" ]] && break
    sleep 3
done
[[ "${session_state:-}" == "ACTIVE" ]] || fail "Bastion session did not become ACTIVE"

known_hosts="$(mktemp)"
tunnel_log="$(mktemp)"
tunnel_pid=""
cleanup() {
    if [[ -n "$tunnel_pid" ]]; then
        kill "$tunnel_pid" >/dev/null 2>&1 || true
        wait "$tunnel_pid" >/dev/null 2>&1 || true
    fi
    rm -f "$known_hosts" "$tunnel_log"
}
trap cleanup EXIT

bastion_host="host.bastion.$OCI_REGION.oci.oraclecloud.com"
ssh \
    -i "$SSH_PRIVATE_KEY_FILE" \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$known_hosts" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -N \
    -L "$LOCAL_PORT:$private_ip:22" \
    -p 22 \
    "$session_id@$bastion_host" >"$tunnel_log" 2>&1 &
tunnel_pid=$!

log "Waiting for the private SSH endpoint through Bastion"
connected=false
for _ in $(seq 1 30); do
    if ! kill -0 "$tunnel_pid" >/dev/null 2>&1; then
        cat "$tunnel_log" >&2
        fail "Bastion SSH tunnel exited before target SSH became reachable"
    fi
    if ssh \
        -i "$SSH_PRIVATE_KEY_FILE" \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$known_hosts" \
        -o ConnectTimeout=5 \
        -p "$LOCAL_PORT" \
        "$TARGET_USER@127.0.0.1" true >/dev/null 2>&1
    then
        connected=true
        break
    fi
    sleep 3
done
[[ "$connected" == "true" ]] || fail "target SSH did not become reachable through Bastion"

git fetch --quiet origin main
release_sha="$(git rev-parse origin/main)"
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve origin/main release SHA"

log "Preparing the acquired host foundation at release $release_sha"
bootstrap_output_file="$(mktemp)"
if ! ssh \
    -i "$SSH_PRIVATE_KEY_FILE" \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$known_hosts" \
    -p "$LOCAL_PORT" \
    "$TARGET_USER@127.0.0.1" \
    "sudo env KATCHA_RELEASE_SHA='$release_sha' KATCHA_OCI_DATA_VOLUME_DEVICE_PATH='$DEVICE_PATH' bash -s" \
    < deploy/scripts/bootstrap-initial-primary.sh |
    tee "$bootstrap_output_file"
then
    rm -f "$bootstrap_output_file"
    fail "remote initial-host foundation bootstrap failed"
fi

fs_uuid="$(sed -n 's/^KATCHA_INITIAL_FS_UUID=//p' "$bootstrap_output_file" | tail -n 1)"
stage="$(sed -n 's/^KATCHA_INITIAL_STAGE=//p' "$bootstrap_output_file" | tail -n 1)"
rm -f "$bootstrap_output_file"

[[ "$stage" == "foundation-ready" ]] || fail "host did not report foundation-ready"
[[ "$fs_uuid" =~ ^[0-9A-Fa-f-]{20,}$ ]] || fail "host returned an invalid filesystem UUID"

gh variable set KATCHA_OCI_DATA_VOLUME_FS_UUID --repo "$REPO" --body "$fs_uuid"
gh variable set KATCHA_OCI_INITIAL_FOUNDATION_READY --repo "$REPO" --body "true"
gh variable set KATCHA_OCI_INITIAL_FOUNDATION_SHA --repo "$REPO" --body "$release_sha"
gh variable set KATCHA_OCI_BASTION_ID --repo "$REPO" --body "$bastion_id"
gh variable set KATCHA_OCI_ADMIN_NSG_ID --repo "$REPO" --body "$admin_nsg_id"
gh variable set KATCHA_OCI_PRIMARY_PRIVATE_IP --repo "$REPO" --body "$private_ip"

log "Initial OCI host foundation is complete"
log "Filesystem UUID recorded in GitHub recovery variables"
log "No production secrets have been copied to the VM yet"
log "KATCHA_OCI_RECOVERY_CONFIGURED remains disabled until full acceptance"
