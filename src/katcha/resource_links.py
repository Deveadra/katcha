"""Canonical application destinations for private, authenticated resources."""
from uuid import UUID


def production_review_url(production_id: UUID, channel_id: UUID | None) -> str | None:
    if channel_id is None:
        return None
    return f"/channels?channel={channel_id}&production={production_id}#content"
