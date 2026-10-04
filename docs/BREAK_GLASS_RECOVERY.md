# Katcha off-OCI break-glass recovery bundle

This runbook creates the encrypted secret escrow required to recover Katcha when
OCI Vault is unavailable or the OCI tenancy/region cannot be trusted as the
only source of bootstrap credentials.

This is **not** a second normal secret store. It is an operator-controlled,
offline recovery artifact.

## What the bundle contains

Exactly four payloads are allowed:

- `katcha.env` — hosted production environment, including the persistent
  `KATCHA_CREDENTIAL_ENCRYPTION_KEY`
- `backup.env` — dedicated R2 backup-writer credentials
- `restore.env` — dedicated R2 read-only recovery credentials
- `aws.tgz` — the existing AWS/Remotion credential-process bundle used by
  hosted rendering

The bundle does **not** contain:

- OCI API private keys
- GitHub personal access tokens
- the `age` private identity used to decrypt the bundle
- Cloudflare R2 bucket-configuration/admin credentials

Those identities stay outside the normal Katcha runtime trust boundary.

## Cryptographic format

Katcha uses the standard `age` file-encryption tool.

The bundle builder:

1. rejects symlinked or non-regular secret inputs
2. creates a deterministic tar structure with no source paths or ownership
3. records SHA-256 and byte length for every payload
4. encrypts the tar archive to one or more public `age` recipients
5. writes the encrypted result with mode `0600`
6. removes the temporary plaintext archive

Extraction:

1. decrypts into a mode-`0600` temporary file
2. rejects absolute paths, traversal, links, duplicate names and unexpected files
3. verifies the manifest, sizes and SHA-256 checksums
4. writes only the four expected payloads plus `manifest.json`
5. writes the destination directory as `0700` and files as `0600`

No secret value is intentionally written to logs.

## One-time offline key setup

Install `age` on a trusted machine.

Create the private identity:

```bash
umask 077
age-keygen -o katcha-breakglass.identity
```

Print the corresponding public recipient:

```bash
age-keygen -y katcha-breakglass.identity
```

Copy `deploy/breakglass/recipients.example` to a local file outside the
repository and replace the placeholder with that public recipient.

You may add multiple public recipients, one per line, so two independent
operators or recovery locations can decrypt the same bundle.

### Protect the private identity

The file `katcha-breakglass.identity` is the decryption key.

Do not:

- commit it
- put it in OCI Vault
- mount it into Katcha
- store it beside the encrypted bundle as the only copy
- paste it into tickets, chat, CI logs or documentation

Keep at least two controlled copies, for example an encrypted password manager
and an offline removable medium stored separately.

Losing every identity means the bundle is unrecoverable. Anyone who obtains an
identity and the encrypted bundle can decrypt the bootstrap secrets.

## Build the encrypted bundle

Start from the same source material that is used to populate the hosted secret
stores.

Example:

```bash
umask 077

python -m katcha.ops.breakglass_bundle create \
  --production-env /secure/katcha/katcha.env \
  --backup-env /secure/katcha/backup.env \
  --restore-env /secure/katcha/restore.env \
  --aws-bundle /secure/katcha/aws.tgz \
  --recipients-file /secure/katcha/recipients.txt \
  --output /secure/katcha/katcha-breakglass.age
```

The command refuses to overwrite an existing output. Create a new file and
replace/archive the previous generation deliberately.

Store the resulting encrypted `.age` file in at least two locations that do
not depend on OCI. The encrypted file itself may be stored in ordinary durable
object/file storage; the private identity is what must remain tightly
controlled.

Recommended copies:

- one operator-controlled encrypted storage location
- one separate offline or second-provider location

The two copies should not share the same single failure domain.

## Verify a bundle before trusting it

Perform a recovery drill on a trusted machine:

```bash
umask 077
mkdir -m 700 /tmp/katcha-breakglass-test

python -m katcha.ops.breakglass_bundle extract \
  --bundle /secure/katcha/katcha-breakglass.age \
  --identity-file /secure/offline/katcha-breakglass.identity \
  --destination /tmp/katcha-breakglass-test
```

Verify that these files exist:

```text
katcha.env
backup.env
restore.env
aws.tgz
manifest.json
```

Then remove the decrypted test directory:

```bash
rm -rf /tmp/katcha-breakglass-test
```

The repository CI performs the same class of roundtrip with ephemeral fixture
keys and fixture secrets. CI never uses the real break-glass identity or real
production secrets.

## Rotation

Rebuild and re-test the encrypted bundle whenever any contained bootstrap
material changes, including:

- Katcha credential-encryption key
- database/control/fence/tunnel credentials
- provider or YouTube credentials stored in the production environment
- R2 backup/restore credentials
- AWS Roles Anywhere/certificate material

Also perform a periodic recovery drill even when no secret changed.

Keep an older encrypted generation only if its credentials are still valid and
the retention is intentional. Do not assume an old bundle remains useful after
credential rotation.

## Critical recovery invariant

The production database can contain provider tokens encrypted with
`KATCHA_CREDENTIAL_ENCRYPTION_KEY`.

A database restore without the matching persistent encryption key can restore
rows successfully while leaving those integrations unusable. Therefore the
production environment and its encryption key are part of the same disaster
recovery generation as the database recovery process.

## Current implementation boundary

This tooling completes the **off-OCI encrypted escrow format and verification
path**.

The normal OCI recovery candidate still retrieves its bootstrap payloads from
OCI Vault.

The next recovery slice will add a controlled no-Vault delivery/bootstrap path
that can consume this verified bundle without embedding long-lived plaintext
secrets in OCI instance metadata or cloud-init user data.

Until that delivery path is merged and exercised, the break-glass bundle is a
validated recovery artifact but does not by itself satisfy the complete
"bootstrap without OCI Vault" production acceptance gate.
