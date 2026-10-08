#!/usr/bin/env bash
set -euo pipefail

# This helper is public software, not recovery secret material. The three
# Roles Anywhere credential files are restored separately from OCI Vault.
DEST_DIR="$1"
if [[ "$#" -ne 1 || ! -d "$DEST_DIR" || -L "$DEST_DIR" ]]; then
    echo "AWS helper destination must be an existing non-symlink directory." >&2
    exit 20
fi

TARGET="$DEST_DIR/aws_signing_helper"
if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    echo "AWS helper destination already exists; refusing to overwrite." >&2
    exit 21
fi

VERSION="1.8.5"
case "$(uname -m)" in
    aarch64|arm64)
        ARCH="Aarch64"
        EXPECTED_SHA="3d131aa888cd56da446f9c6bb460b1f0569f6c7edc74eae6193a2fe3928883ba"
        ;;
    x86_64|amd64)
        ARCH="X86_64"
        EXPECTED_SHA="beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d"
        ;;
    *)
        echo "Unsupported architecture for pinned AWS Roles Anywhere helper." >&2
        exit 22
        ;;
esac

URL="https://rolesanywhere.amazonaws.com/releases/$VERSION/$ARCH/Linux/Amzn2023/aws_signing_helper"
TEMP="$(mktemp "$DEST_DIR/.aws_signing_helper.XXXXXXXX")"
trap 'rm -f "$TEMP"' EXIT

curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    --retry 3 --retry-delay 2 \
    --connect-timeout 10 --max-time 120 \
    --output "$TEMP" "$URL"

if ! printf '%s  %s\n' "$EXPECTED_SHA" "$TEMP" | sha256sum --check --status; then
    echo "AWS helper SHA-256 mismatch; refusing recovery startup." >&2
    exit 23
fi

chmod 0755 "$TEMP"
mv -- "$TEMP" "$TARGET"
trap - EXIT
echo "AWS_ROLES_ANYWHERE_HELPER_VERIFIED version=$VERSION arch=$ARCH"
