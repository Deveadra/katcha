from katcha.api.main import app


def test_clip_library_lifecycle_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/clips/library",
        "/v1/clips/library/summary",
        "/v1/clips/{clip_id}/media",
        "/v1/clips/{clip_id}/sources",
        "/v1/clips/{clip_id}/library-state",
        "/v1/clips/{clip_id}/library-metadata",
        "/v1/clips/{clip_id}/archive",
        "/v1/clips/{clip_id}/restore",
        "/v1/clips/{clip_id}/purge",
        "/v1/channels/{channel_profile_id}/clip-retention",
        "/v1/channels/{channel_profile_id}/clip-retention/preview",
        "/v1/channels/{channel_profile_id}/clip-retention/run",
    }
    assert expected <= set(paths)
    assert "get" in paths["/v1/clips/{clip_id}/media"]
    assert "patch" in paths["/v1/clips/{clip_id}/library-metadata"]
    assert "post" in paths["/v1/clips/{clip_id}/purge"]
    assert "put" in paths["/v1/channels/{channel_profile_id}/clip-retention"]
