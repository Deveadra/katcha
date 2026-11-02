# Twelve Labs Jockey: deployable, operator-gated Editorial workflow

The SDK and initial read-only adapter were introduced in PR #570. The follow-on
Editorial integration adds durable, manually advanced provider indexing and
semantic retrieval over Katcha's **existing canonical Clip/R2, Media Bin, local
scene index, preparation manifest, rights and revision authority**.

**No remote provider calls are made by installing or deploying.** The operator
must enable dispatch, configure a separate store per channel, authorize each
request and set a per-project call ceiling. Jockey results are review-only,
never verified semantic facts, footage rights, shot selections or publish consent.

## Deployment and activation

The API and analysis images install the pinned official `twelvelabs` extra.
Use the existing supervised Compose configuration to inject
`/etc/katcha/katcha.env` via `KATCHA_ENV_FILE`. The API service must receive
the token because operator API requests make the bounded provider calls.
Never copy or echo credentials into the repository or a support ticket.

Example private environment entries:

```sh
KATCHA_TWELVELABS_JOCKEY_API_TOKEN=<already in /etc/katcha/katcha.env>
KATCHA_TWELVELABS_JOCKEY_DISPATCH_ENABLED=false
KATCHA_TWELVELABS_JOCKEY_MAX_OPERATIONS_PER_PROJECT=0
KATCHA_TWELVELABS_JOCKEY_KNOWLEDGE_STORES={}
```

After deployment, create a knowledge store for each channel in the Twelve Labs
dashboard (or official SDK), set `KNOWLEDGE_STORES` to a JSON map of canonical
channel UUIDs to **distinct** store IDs, set the per-project operation ceiling
and enable dispatch. Apply changed environment settings using the existing
supervisor restart procedure. **This is configuration, not a new image build.**
Keep dispatch off until provider pricing, approved rights, storage, deletion and
billing policies are reviewed. Provider operation ceilings count **requests**,
not USD: provider billing is not guaranteed to stay under any dollar cap.
Inspect Twelve Labs usage and charges before increasing a ceiling.

The initial local-only readiness check remains:

```sh
python -m katcha.integrations.twelvelabs_jockey
```

It reports no token value and makes no external requests.

## Operator API workflow

All routes require the existing channel-scoped Katcha control authentication.
Paths below are relative to
`/v1/channels/{channel_uuid}/editorial-projects/{project_uuid}/jockey`.

1. Add a **canonical, permitted video** to the project's Media Bin. Its local
   measured scene index must be completed before semantic search. Uploading
   directly to Twelve Labs is not a substitute for canonical Clip/R2 integrity.
2. `POST /media/{media_item_uuid}/index` with
   `{"expected_revision":1,"external_inspection_authorized":true,"provider_spend_approved":true}`.
   This stages and SHA-256-verifies up to **190 MB** of canonical media and makes
   **one** direct asset upload. It records a committed attempt **before** the
   provider call. The request is idempotent for that clip, project and store.
3. `POST /media/{media_item_uuid}/advance` with the same authorization body.
   When the asset is ready, it records `asset_ready`. Call again to add it
   to the channel's knowledge store, then periodically call again to inspect
   the item's asynchronous processing status. No automatic provider retries.
   `GET /media/{media_item_uuid}` returns the saved receipt.
4. Prepare the project's saved script and select a footage requirement and up
   to eight distinct indexed Media Bin videos. Call `POST /search` with:

```json
{
  "expected_revision": 1,
  "external_inspection_authorized": true,
  "provider_spend_approved": true,
  "preparation_run_id": "UUID",
  "requirement_id": "requirement-id-from-preparation",
  "item_ids": ["MEDIA_ITEM_UUID"],
  "idempotency_key": "operator-controlled-unique-key",
  "max_results": 8
}
```

Search makes **one** Jockey request against the channel's dedicated knowledge
store, constrained to **ready, current, permitted, SHA-verified canonical clips**.
The result attaches provider moment suggestions to measured local shot IDs and
keyframe hashes by timestamp overlap. No model-generated timestamp becomes
authoritative. The response never sets `verified_fact`, `rights_granted`
or `production_eligible` to true. Use the existing measured shot-proposals,
human choice and Storyboard approval workflow for actual binding; this search
does **not** silently rewrite Storyboard or bypass render permission checks.

`GET /search/{run_uuid}` reads the saved result and rechecks the current
measured source snapshot. A changed script, media or rights receipt blocks
stale evidence.

## Provider billing and uncertain outcomes

Each asset creation, knowledge-store item creation and search reserves one
durable operation in the existing `EditorialRun` ledger, serialized by the
project lock. Set `KATCHA_TWELVELABS_JOCKEY_MAX_OPERATIONS_PER_PROJECT` to a
small nonzero ceiling only after approving provider spend. The receipt exposes
the number of reserved operations and the provider identifiers, never the key.
An SDK timeout, lost response or database checkpoint failure leaves an
**unknown** outcome and permanently blocks automatic replay. Inspect provider
usage and reconcile any orphaned remote asset manually before planning a new
operation. Read-only status polls do not create new remote assets or items.

The ledger tracks attempted chargeable operations, **not actual USD charges**.
Twelve Labs usage/billing remains the authoritative charge source. This is not
a claim of hard USD budget enforcement.

## Limitations and acceptance

- Provider indexing and search are operator-initiated HTTP operations, not
  continuous background synchronization. A future automatic scheduler can use
  the same persisted receipts and gates, but is not required to index/search
  footage manually after deployment.
- Direct upload uses the documented SDK path and accepts videos below 190 MB.
  Longer files need a separately reviewed multipart strategy.
- Semantic retrieval does not verify actors, dialogue or events by itself.
  The measured overlap proves only a decoded source-relative shot interval.
- The API may take time during upload; use an operator client with suitable
  request timeouts. A timeout **must not** be blindly retried.
- No real provider traffic, costs, production media rights, live deployment or
  live quality benchmark are claimed by CI. Benchmark licensed footage and
  negative examples before allowing production decisions.

## Offline verification

```sh
python -m pytest tests/test_editorial_jockey.py tests/test_twelvelabs_jockey.py -q
python -m ruff check src/katcha/services/editorial_jockey.py src/katcha/api/editorial_jockey.py tests/test_editorial_jockey.py
```

Official provider reference:
https://docs.twelvelabs.io/agents/get-started/quickstart/search-a-knowledge-store
