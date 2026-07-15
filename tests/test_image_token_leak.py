"""Regression: the Drive OAuth token must never be attached to a non-Google
image host (codex R3 #1 — token exfiltration via a substring hostname check)."""

import pytest


@pytest.fixture
def engine():
    import skrepka._engine as e
    return e


def test_token_attached_to_real_google_hosts(engine):
    for url in (
        "https://lh3.googleusercontent.com/abc",
        "https://googleusercontent.com/x",
        "https://drive.google.com/uc?id=123",
        "https://docs.google.com/img",
    ):
        assert engine._is_google_auth_image_host(url), url


def test_token_NOT_attached_to_deceptive_hosts(engine):
    for url in (
        "https://google.com.attacker.example/x",
        "https://googleusercontent.com.evil.example/x",
        "https://attacker.example/?from=googleusercontent.com",
        "https://attacker.example/google.com/img",
        "http://lh3.googleusercontent.com/x",          # not https
        "https://user:pw@lh3.googleusercontent.com/x",  # userinfo
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
    ):
        assert not engine._is_google_auth_image_host(url), url


def test_no_fetch_for_external_or_private_hosts(engine, monkeypatch):
    """The SSRF guard must not even ISSUE a request to a non-Google host —
    predicate-only checks miss the actual network call (codex R3 #2)."""
    import requests
    calls = []

    class FakeResp:
        status_code = 200
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"\x89PNG\r\n\x1a\n"

    def fake_get(url, **kw):
        calls.append(url)
        assert kw.get("allow_redirects") is False  # redirects disabled
        return FakeResp()

    monkeypatch.setattr(requests, "get", fake_get)

    html = (
        '<img src="http://169.254.169.254/latest/meta-data/">'
        '<img src="https://google.com.attacker.example/x.png">'
        '<img src="https://attacker.example/?u=googleusercontent.com">'
        '<img src="https://lh3.googleusercontent.com/legit.png">'
    )
    class C:
        token = "TESTTOKEN"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out, count = engine._download_images_from_html(html, d + "/imgs", C())
    # only the genuine Google host was fetched
    assert calls == ["https://lh3.googleusercontent.com/legit.png"]
    assert "169.254.169.254" not in "".join(calls)
    assert count == 1
