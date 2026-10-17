from katcha.integrations.download import canonicalize_url, detect_platform


def test_platform_detection() -> None:
    assert detect_platform("https://www.tiktok.com/@x/video/1") == "tiktok"
    assert detect_platform("https://instagram.com/reel/abc") == "instagram"
    assert detect_platform("https://youtu.be/abc") == "youtube"
    assert detect_platform("https://reddit.com/r/funny/comments/abc") == "reddit"
    assert detect_platform("https://example.com/video.mp4") == "generic"


def test_canonical_url_drops_fragment_and_normalizes_host() -> None:
    assert canonicalize_url("HTTPS://WWW.Example.com/x?a=1#comments") == (
        "https://www.example.com/x?a=1"
    )


def test_platform_detection_does_not_trust_hostname_substrings():
    assert detect_platform("https://youtube.com.example.invalid/video") == "generic"
    assert detect_platform("https://example.invalid/youtube.com") == "generic"


def test_youtube_bot_challenge_has_actionable_non_retryable_diagnostic(tmp_path, monkeypatch):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader
    from katcha.orchestration.failure_details import failure_details

    class Challenge:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you’re not a bot")

    monkeypatch.setattr(downloader, "YoutubeDL", Challenge)
    with pytest.raises(downloader.DownloadAuthenticationRequired) as caught:
        downloader.download("https://youtu.be/fixture", Settings(_env_file=None, work_dir=tmp_path))
    detail = failure_details(caught.value)
    assert detail["code"] == "youtube_authentication_required"
    assert detail["retryable"] is False
    assert "Katcha server" in detail["recovery"]
    assert not list(tmp_path.iterdir())


def test_server_cookies_use_private_copy_and_never_become_media(tmp_path, monkeypatch):
    import os

    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    source = tmp_path / "operator.cookies"
    source.write_text("fixture session; not a real credential")
    work = tmp_path / "work"
    copies = []

    class CookieOnly:
        def __init__(self, options):
            from pathlib import Path

            cookie = Path(options["cookiefile"])
            copies.append(cookie)
            assert cookie != source
            assert cookie.read_text() == source.read_text()
            assert os.stat(cookie).st_mode & 0o777 == 0o600
            assert os.stat(cookie.parent).st_mode & 0o777 == 0o700
            cookie.write_text("fixture updated jar")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            return {"id": "fixture"}

    monkeypatch.setattr(downloader, "YoutubeDL", CookieOnly)
    with pytest.raises(downloader.MediaDownloadError, match="no media file"):
        downloader.download(
            "https://youtu.be/fixture",
            Settings(_env_file=None, work_dir=work, youtube_download_cookie_file=source),
        )
    assert source.read_text() == "fixture session; not a real credential"
    assert not copies[0].exists()
