"""Proof that the purge guard and UI inspector agree for every work type."""
import uuid

import pytest
from fastapi import HTTPException

from katcha import db
from katcha.api.clip_library import get_clip_blocking_references
from katcha.longform_models import Compilation
from katcha.production_models import Production
from katcha.services.clip_lifecycle import _assert_not_active, clip_reference_summary
from katcha.short_episode_models import ShortEpisode


def test_blockers_are_real_work_with_actionable_deep_links(fixture_work):
    clip_id, channel, other, ids = fixture_work
    payload = get_clip_blocking_references(clip_id)
    assert payload.active_count == 3
    refs = {item.kind: item for item in payload.active}
    assert set(refs) == {"production", "short_episode", "compilation"}
    assert ids["old"] not in {item.id for item in payload.active}
    assert refs["production"].id == ids["production"]
    assert refs["production"].channel_name == "Forescene"
    assert refs["production"].href == (
        f"/channels?channel={channel}&production={ids['production']}#content"
    )
    assert refs["short_episode"].title == "Ranked highlights"
    assert refs["short_episode"].href == (
        f"/studio?channel={channel}&episode={ids['short_episode']}"
    )
    assert refs["compilation"].channel_name == "RankSnaxx"
    assert refs["compilation"].href == (
        f"/operations?channel={other}&work={ids['compilation']}"
    )
    with db.session_scope() as session:
        assert clip_reference_summary(session, clip_id).active_count == 3


def test_completed_work_unlocks_purge_guard(fixture_work):
    clip_id, _, _, ids = fixture_work
    with db.session_scope() as session:
        session.get(Production, ids["production"]).status = "approved"
        session.get(ShortEpisode, ids["short_episode"]).status = "published"
        session.get(Compilation, ids["compilation"]).status = "approved"
    assert get_clip_blocking_references(clip_id).active_count == 0
    with db.session_scope() as session:
        assert _assert_not_active(session, clip_id).active_count == 0


def test_missing_clip_is_not_reported_as_ready(fixture_work):
    with pytest.raises(HTTPException) as caught:
        get_clip_blocking_references(uuid.uuid4())
    assert caught.value.status_code == 404
