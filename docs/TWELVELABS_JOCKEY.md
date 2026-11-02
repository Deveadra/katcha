# TwelveLabs Jockey SDK: guarded Katcha integration

This is a **first integration slice**, not production activation or semantic
benchmark acceptance. It extends Katcha's existing Editorial/Clip/Temporal
pipeline, rather than replacing its measured scene index or rights decisions.

## Install and secret loading

The optional dependency is pinned to `twelvelabs==1.3.6` in
`pyproject.toml` under `[project.optional-dependencies].twelvelabs`.
`Dockerfile.analysis` installs that extra during its normal image build.
For a local development environment:

```sh
python -m pip install -e '.[twelvelabs]'
python -m katcha.integrations.twelvelabs_jockey
```

The second command is **local-only** and reports whether the SDK, token,
dispatch flag and channel mappings are configured. It does not call TwelveLabs,
upload footage, index media or incur an API charge. No key value is printed.

The operator's token is already stored at
`/etc/katcha/katcha.env` under
`KATCHA_TWELVELABS_JOCKEY_API_TOKEN`. **Do not copy it into the repo**.
Katcha's settings read process environment or the local `.env`; the host
file must actually be injected into the intended service. The existing Compose
stack uses `env_file: ${KATCHA_ENV_FILE:-.env}`, so production operators should
verify that the supervised launch sets `KATCHA_ENV_FILE` to the private file
and that the **analysis** container receives it. Never print the token in
diagnostics, shell history, CI logs or a PR.

The two additional optional settings are:

```sh
KATCHA_TWELVELABS_JOCKEY_DISPATCH_ENABLED=false
KATCHA_TWELVELABS_JOCKEY_KNOWLEDGE_STORES={}
```

`KNOWLEDGE_STORES` is a JSON object keyed by canonical Katcha channel UUID,
with each value a distinct TwelveLabs knowledge-store ID. Do not share a
provider store between channels. Leave dispatch **false** until the provider
budget, source inspection permission, media rights, and durable request receipt
are reviewed. The token alone never enables traffic.

## Integration contract

`katcha.integrations.twelvelabs_jockey.search_candidates` wraps the official
`client.knowledge_stores.search` API. It requires:

- Explicit external-inspection authorization and an enabled operator gate.
- A channel-specific knowledge-store mapping and a nonempty provider token.
- A bounded text query and explicit provider-item -> canonical Clip UUID map
  built by the caller from **current, rights-inspected Katcha records**.
- A caller-managed durable provider submission/budget receipt, including
  reconciliation of unknown outcomes before any retry.

Only allowlisted provider item IDs are returned. Search intervals are
**unverified source-relative hints**, not exact decoded PTS. The result is
review-only: no auto-binding, scene selection, render eligibility, rights grant,
or publishing. An unknown provider result must not trigger automatic retries.

This adapter is deliberately **read-only**. No new video upload, knowledge-store
creation, remote indexing, worker dispatch, or Temporal workflow has been added.
Those operations require a separate integration slice that reuses Katcha's
existing canonical R2/Clip records, durable EditorialRun/provider receipts,
cost reservation and rights/permission gates. In particular, an upload can be
processed asynchronously and a knowledge-store item must become `ready`
before it can be searched.

## Offline verification and remaining acceptance

```sh
python -m pytest tests/test_twelvelabs_jockey.py -q
python -m ruff check src/katcha/integrations/twelvelabs_jockey.py tests/test_twelvelabs_jockey.py src/katcha/config.py
```

These are mocked contracts, **not** proof of live SDK connectivity, usable
media, search quality or a working E2E editorial pipeline.

Before production activation, add a durable channel-scoped asset-to-provider
mapping, asynchronous asset/index reconciliation, provider cost ledger/unknown
outcome handling, and independent verification of timestamps/actions against
canonical footage. Then benchmark licensed clips with hard negatives against
Katcha's existing semantic baseline. Obtain explicit approval before uploading
third-party media, incurring paid inference, deploying or publishing.

Official docs:
- https://docs.twelvelabs.io/agents/get-started/quickstart/search-a-knowledge-store
- https://docs.twelvelabs.io/agents/get-started/quickstart/create-a-response
- https://docs.twelvelabs.io/agents/troubleshooting/error-handling
- https://pypi.org/project/twelvelabs/1.3.6/
