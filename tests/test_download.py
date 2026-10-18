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


def test_session_success_removes_cookie_before_returning_media(tmp_path, monkeypatch):
    from pathlib import Path

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    cookie = tmp_path / "operator.cookies"
    cookie.write_text("fixture-only session")
    copies = []

    class SessionVideo:
        def __init__(self, options):
            self.cookie = options.get("cookiefile")
            self.path = Path(options["outtmpl"]).parent / "video.mp4"
            assert options["js_runtimes"]["node"] == {}
            assert options["socket_timeout"] == 30
            if self.cookie:
                copies.append(Path(self.cookie))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if self.cookie:
                Path(self.cookie).write_text("updated fixture session")

        def extract_info(self, *args, **kwargs):
            if not self.cookie:
                raise RuntimeError("Sign in to confirm you're not a bot")
            self.path.write_bytes(b"fixture media")
            return {"id": "video"}

    monkeypatch.setattr(downloader, "YoutubeDL", SessionVideo)
    monkeypatch.setattr(downloader, "ffprobe", lambda path: {"duration": 1})
    result = downloader.download("https://youtu.be/fixture", Settings(
        _env_file=None, work_dir=tmp_path / "work", youtube_download_cookie_file=cookie,
    ))
    assert result.source_metadata["youtube_access_mode"] == "session_fallback"
    assert result.path.read_bytes() == b"fixture media"
    assert copies and all(not path.exists() for path in copies)
    assert cookie.read_text() == "fixture-only session"
    assert list(result.path.parent.iterdir()) == [result.path]


def test_approved_route_is_anonymous_and_cleans_public_fragments(tmp_path, monkeypatch):
    from pathlib import Path

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    attempts = []

    class RoutedVideo:
        def __init__(self, options):
            attempts.append(dict(options))
            self.route = options.get("proxy")
            self.directory = Path(options["outtmpl"]).parent
            assert "cookiefile" not in options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            if not self.route:
                (self.directory / "failed.part").write_text("partial")
                raise RuntimeError("Sign in to confirm you're not a bot")
            assert not (self.directory / "failed.part").exists()
            (self.directory / "video.mp4").write_bytes(b"fixture video")
            return {"id": "fixture"}

    monkeypatch.setattr(downloader, "YoutubeDL", RoutedVideo)
    monkeypatch.setattr(downloader, "ffprobe", lambda path: {"duration": 1})
    result = downloader.download("https://youtu.be/fixture", Settings(
        _env_file=None, work_dir=tmp_path,
        youtube_download_proxy_url="socks5://127.0.0.1:1080",
        youtube_download_cookie_file=tmp_path / "must-not-be-read.cookies",
    ))
    assert len(attempts) == 2
    assert "proxy" not in attempts[0]
    assert attempts[1]["proxy"] == "socks5://127.0.0.1:1080"
    assert result.source_metadata["youtube_access_mode"] == "approved_route"
    assert "proxy" not in str(result.source_metadata)


def test_route_failure_is_bounded_and_never_exposes_credentials(tmp_path, monkeypatch, capsys):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader
    from katcha.orchestration.failure_details import failure_details

    attempts = []

    class FailingRoute:
        def __init__(self, options):
            attempts.append(options)
            self.route = options.get("proxy")
            if self.route:
                options["logger"].error("proxy password=fixture-secret")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            if self.route:
                raise RuntimeError("connection failed: socks5://user:fixture-secret@host:1080")
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "YoutubeDL", FailingRoute)
    with pytest.raises(downloader.DownloadRouteUnavailable) as caught:
        downloader.download("https://youtu.be/fixture", Settings(
            _env_file=None, work_dir=tmp_path,
            youtube_download_proxy_url="socks5://user:fixture-secret@host:1080",
        ))
    assert len(attempts) == 2
    detail = failure_details(caught.value)
    assert detail["code"] == "youtube_download_route_unavailable"
    assert detail["retryable"] is False
    assert "fixture-secret" not in str(detail)
    assert "fixture-secret" not in str(capsys.readouterr())
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert not list(tmp_path.iterdir())


def test_download_route_configuration_is_private_and_validated():
    import pytest

    from katcha.config import Settings

    settings = Settings(_env_file=None, youtube_download_proxy_url="https://user:secret@proxy:443")
    assert "user:secret" not in repr(settings)
    for url in ("file:///tmp/proxy", "http://", "https://proxy/path", "http://proxy:99999"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, youtube_download_proxy_url=url)


def test_configured_route_is_not_used_for_other_errors_or_platforms(tmp_path, monkeypatch):
    import pytest

    from katcha.config import Settings
    from katcha.integrations import download as downloader

    attempts = []
    message = "HTTP Error 429"

    class NoRoute:
        def __init__(self, options):
            attempts.append(options)
            assert "proxy" not in options
            assert "cookiefile" not in options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            raise RuntimeError(message)

    monkeypatch.setattr(downloader, "YoutubeDL", NoRoute)
    settings = Settings(_env_file=None, work_dir=tmp_path,
                        youtube_download_proxy_url="http://proxy:8080")
    with pytest.raises(downloader.MediaDownloadError):
        downloader.download("https://youtu.be/fixture", settings)
    message = "Sign in to confirm you're not a bot"
    with pytest.raises(downloader.MediaDownloadError):
        downloader.download("https://example.com/watch?v=fixture", settings)
    assert len(attempts) == 2
    assert not list(tmp_path.iterdir())
