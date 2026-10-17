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

            self.job_dir = Path(options["outtmpl"]).parent
            self.cookie = options.get("cookiefile")
            if self.cookie:
                cookie = Path(self.cookie)
                copies.append(cookie)
                assert cookie != source
                assert cookie.read_text() == source.read_text()
                assert os.stat(cookie).st_mode & 0o777 == 0o600
                assert os.stat(cookie.parent).st_mode & 0o777 == 0o700
                assert not (self.job_dir / "failed-public.part").exists()
                cookie.write_text("fixture updated jar")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            if not self.cookie:
                (self.job_dir / "failed-public.part").write_text("partial")
                raise RuntimeError("Sign in to confirm you're not a bot")
            return {"id": "fixture"}

    monkeypatch.setattr(downloader, "YoutubeDL", CookieOnly)
    with pytest.raises(downloader.MediaDownloadError, match="no media file"):
        downloader.download(
            "https://youtu.be/fixture",
            Settings(_env_file=None, work_dir=work, youtube_download_cookie_file=source),
        )
    assert source.read_text() == "fixture session; not a real credential"
    assert not copies[0].exists()


def test_cookie_parse_error_does_not_expose_cookie_rows(tmp_path, monkeypatch):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader
    from katcha.orchestration.failure_details import failure_details

    class CookieLoadError(RuntimeError):
        pass
    class InvalidSession:
        def __init__(self, options):
            if "cookiefile" in options:
                raise CookieLoadError(
                    "invalid Netscape row: .youtube.com TRUE / FALSE bad SID secretvalue"
                )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you're not a bot")
    cookie = tmp_path / "session.cookies"
    cookie.write_text("offline malformed fixture")
    monkeypatch.setattr(downloader, "YoutubeDL", InvalidSession)
    with pytest.raises(downloader.DownloadSessionConfigurationError) as caught:
        downloader.download("https://youtu.be/fixture", Settings(
            _env_file=None, work_dir=tmp_path / "work", youtube_download_cookie_file=cookie))
    detail = failure_details(caught.value)
    assert "secretvalue" not in str(detail)
    assert detail["code"] == "youtube_download_session_invalid"
    assert detail["retryable"] is False
    assert caught.value.__suppress_context__ is True


def test_public_youtube_download_does_not_need_configured_session(tmp_path, monkeypatch):
    from pathlib import Path

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    seen = []

    class PublicVideo:
        def __init__(self, options):
            seen.append(dict(options))
            assert "cookiefile" not in options
            self.path = Path(options["outtmpl"]).parent / "public.mp4"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            self.path.write_bytes(b"test media")
            return {"id": "public", "title": "Public fixture"}

    monkeypatch.setattr(downloader, "YoutubeDL", PublicVideo)
    monkeypatch.setattr(downloader, "ffprobe", lambda path: {"duration": 1})
    # Missing session files should not break normal public URL acquisition.
    result = downloader.download("https://youtu.be/public", Settings(
        _env_file=None, work_dir=tmp_path / "work",
        youtube_download_cookie_file=tmp_path / "missing-session.cookies",
    ))
    assert len(seen) == 1
    assert result.source_metadata["youtube_access_mode"] == "public"
    assert result.path.read_bytes() == b"test media"
    import shutil
    shutil.rmtree(result.path.parent)


def test_unrelated_youtube_download_error_does_not_read_session(tmp_path, monkeypatch):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    attempts = []

    class OtherError:
        def __init__(self, options):
            assert "cookiefile" not in options
            attempts.append(dict(options))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(downloader, "YoutubeDL", OtherError)
    with pytest.raises(downloader.MediaDownloadError) as caught:
        downloader.download("https://youtu.be/public", Settings(
            _env_file=None, work_dir=tmp_path / "work",
            youtube_download_cookie_file=tmp_path / "missing-session.cookies",
        ))
    assert not isinstance(caught.value, downloader.DownloadAuthenticationRequired)
    assert len(attempts) == 1
    assert not list((tmp_path / "work").iterdir())


def test_youtube_challenge_tries_session_at_most_once(tmp_path, monkeypatch):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader
    from katcha.orchestration.failure_details import failure_details

    cookie = tmp_path / "authorized.cookies"
    cookie.write_text("fixture session")
    attempts = []

    class AlwaysChallenged:
        def __init__(self, options):
            attempts.append("session" if "cookiefile" in options else "public")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "YoutubeDL", AlwaysChallenged)
    with pytest.raises(downloader.DownloadAuthenticationRequired) as caught:
        downloader.download("https://youtu.be/public", Settings(
            _env_file=None, work_dir=tmp_path / "work",
            youtube_download_cookie_file=cookie,
        ))
    assert attempts == ["public", "session"]
    assert failure_details(caught.value)["retryable"] is False
    assert cookie.read_text() == "fixture session"
    assert not list((tmp_path / "work").iterdir())
