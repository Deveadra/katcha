from urllib.parse import parse_qs, urlparse

from katcha.config import Settings
from katcha.integrations.youtube.oauth import (
    MONETARY_SCOPE,
    build_authorization_url,
    requested_scopes,
)


def test_requested_scopes_make_monetary_access_opt_in() -> None:
    basic = Settings(_env_file=None, youtube_include_monetary_scope=False)
    monetary = Settings(_env_file=None, youtube_include_monetary_scope=True)

    assert MONETARY_SCOPE not in requested_scopes(basic)
    assert MONETARY_SCOPE in requested_scopes(monetary)


def test_authorization_url_carries_pkce_state_and_offline_access() -> None:
    scopes = ("scope-a", "scope-b")
    url = build_authorization_url(
        client_id="client-id",
        redirect_uri="https://example.test/callback",
        state="state-value",
        code_challenge="challenge-value",
        scopes=scopes,
    )
    query = parse_qs(urlparse(url).query)

    assert query["state"] == ["state-value"]
    assert query["code_challenge"] == ["challenge-value"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == ["scope-a scope-b"]
