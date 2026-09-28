from katcha.api.main import app


def test_clip_library_media_route_is_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/clips/{clip_id}/media" in paths
    assert "get" in paths["/v1/clips/{clip_id}/media"]
