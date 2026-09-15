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
