import asyncio
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from katcha.api import main as api_main
from katcha.config import Settings
from katcha.integrations.youtube.oauth import (
    DEFAULT_YOUTUBE_OAUTH_RETURN_TO,
    MONETARY_SCOPE,
    _oauth_state,
    build_authorization_url,
    normalize_youtube_oauth_return_to,
    requested_scopes,
    youtube_oauth_return_to,
)
from katcha.services.channel_profiles import _connection_profile_metadata


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



def test_oauth_state_round_trips_katcha_launcher_return_target() -> None:
    return_to = "http://localhost:8765/channels?setup=1"
    state = _oauth_state(return_to)

    assert youtube_oauth_return_to(state) == return_to


def test_oauth_return_target_rejects_external_redirects() -> None:
    assert (
        normalize_youtube_oauth_return_to(
            "https://attacker.example/channels?setup=1"
        )
        == DEFAULT_YOUTUBE_OAUTH_RETURN_TO
    )
    assert (
        youtube_oauth_return_to("legacy-state-without-return-target")
        == DEFAULT_YOUTUBE_OAUTH_RETURN_TO
    )


def test_oauth_callback_redirects_success_back_to_channel_studio(monkeypatch) -> None:
    connection_id = uuid.uuid4()
    state = _oauth_state("http://localhost:8765/channels?setup=1")
    monkeypatch.setattr(
        api_main,
        "complete_youtube_oauth",
        lambda **_kwargs: SimpleNamespace(id=connection_id),
    )

    response = asyncio.run(
        api_main.youtube_oauth_callback(
            state=state,
            code="fixture-code",
        )
    )

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert target.scheme == "http"
    assert target.netloc == "localhost:8765"
    assert target.path == "/channels"
    assert query["setup"] == ["1"]
    assert query["youtube"] == ["connected"]
    assert query["connection"] == [str(connection_id)]


def test_oauth_callback_redirects_google_error_back_to_channel_studio() -> None:
    state = _oauth_state("http://127.0.0.1:8765/channels?setup=1")

    response = asyncio.run(
        api_main.youtube_oauth_callback(
            state=state,
            error="access_denied",
        )
    )

    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert target.netloc == "127.0.0.1:8765"
    assert query["youtube"] == ["error"]
    assert "access_denied" in query["message"][0]



def test_channel_profile_metadata_extracts_nested_youtube_handle() -> None:
    connection = SimpleNamespace(
        channel_id="UC-fixture",
        channel_title="FORESCENE",
        connection_metadata={
            "channel_response": {
                "items": [
                    {
                        "snippet": {
                            "title": "FORESCENE",
                            "customUrl": "@forescene",
                        }
                    }
                ]
            }
        },
    )

    metadata = _connection_profile_metadata(connection)

    assert metadata["channel_title"] == "FORESCENE"
    assert metadata["channel_handle"] == "@forescene"
    assert metadata["custom_url"] == "@forescene"
