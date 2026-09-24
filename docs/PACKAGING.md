# Publication packaging

Katcha treats YouTube packaging as versioned publication lineage. A packaging variant is
immutable; activating it is a separate durable operation.

## P9.1 boundary

This slice supports:

- a title and description;
- an optional PNG or JPEG thumbnail stored under a publication/version namespace;
- immutable thumbnail size, MIME type and SHA-256;
- manual, idempotent activation on a publication that already has a YouTube video ID;
- durable title/thumbnail activation history.

It does **not** automatically choose a variant, rotate packages, or infer a winner. Reach
and click-through reporting belongs to P9.2, and learned recommendations belong to P9.3.

## Object layout

A thumbnail must already exist in Katcha's object store under:

```
packaging/<publication-id>/<variant-key>/v<version>/<filename>.png
```

JPEG files may use `.jpg` or `.jpeg`. Katcha reads the object when the variant is
created, verifies the actual image signature and YouTube's current size ceiling, then
freezes its SHA-256. Activation fails if the object bytes later differ. Do not overwrite
a variant's object; create a new variant version.

## API flow

Create an immutable variant:

```http
POST /v1/publications/{publication_id}/packaging/variants
```

Example body:

```json
{
  "variant_key": "hook-a",
  "version": 1,
  "title": "The ending nobody saw coming",
  "description": "The original publication description.",
  "thumbnail_storage_key": "packaging/<publication-id>/hook-a/v1/thumbnail.png",
  "created_by": "operator"
}
```

Activate it:

```http
POST /v1/publications/{publication_id}/packaging/activations
```

```json
{
  "variant_id": "<variant-uuid>",
  "idempotency_key": "manual-hook-a-v1"
}
```

List the immutable variant and activation history with the corresponding GET routes.

## Mutation semantics

Activation uses the existing publishing Temporal task queue and the publication's existing
YouTube OAuth connection.

1. Validate publication, channel connection, frozen variant and video ID.
2. Update the YouTube snippet while preserving the publication's tags and category.
3. If present, verify the stored thumbnail still matches the frozen size/hash and upload it.
4. Mark the activation applied and freeze the active packaging lineage into
   `Publication.treatment_metadata.active_packaging`.

Provider mutations have one automatic attempt. If the network dies after YouTube may have
accepted a mutation, Katcha records the failure instead of blindly spending provider quota
again. A new explicit activation key is required for operator recovery. If the title and
description already match on recovery, the text mutation is skipped locally.

Legacy publications with no packaging variants continue through the existing publishing
workflow unchanged.

## Future performance loop

P9.2 will ingest YouTube reach reporting (thumbnail impressions and CTR) and attribute
maturity-matched reach to the active packaging interval. P9.3 may recommend variants only
after channel-scoped chronological validation. CTR alone will never be treated as a winner:
watch quality, retention and contribution margin remain part of the decision.
