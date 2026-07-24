"""logout / revoke / forget: local artifact removal, the revoke result matrix
(only an unambiguous success drops the local token; the refresh token never
leaves the POST form body), destructive-command confirmation gating, and
forget's honest dry-run/kept accounting (R3 item 2)."""

import json
import os
import sys

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path
    os.chmod(home, 0o700)
    cfg = home / "cfg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(cfg))
    import skrepka.config as config
    config.ensure_config_dir()
    return config


@pytest.fixture
def priv():
    import skrepka.privacy as p
    return p


def _last_json(capsys):
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def _seed_token(store, refresh="RT"):
    store.write_token_envelope({"refresh_token": refresh, "token": "AT"},
                               {"granted_scopes": ["s"]})


def _no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin",
                        type("F", (), {"isatty": lambda self: False})())


# --- logout ---------------------------------------------------------------

def test_logout_removes_tokens_keeps_credentials(store, priv, capsys):
    _seed_token(store)
    store.write_secret_bytes(store.CREDENTIALS_NAME, b'{"installed":{}}')
    store.write_secret_bytes("smktok-abc.json", b'{"token":{}}')
    store.write_secret_bytes("smoke-abc.json", b'{"state":"x"}')

    rc = priv.cmd_logout([])
    assert rc == 0
    out = _last_json(capsys)
    assert out["status"] == "signed_out"
    assert store.TOKEN_NAME in out["removed"]
    assert "smktok-abc.json" in out["removed"]
    # credentials kept for a fast reinit; the smoke JOURNAL is left (a later
    # init reconciles it once its bound token is gone)
    assert store.read_secret_bytes(store.CREDENTIALS_NAME) is not None
    assert store.read_secret_bytes(store.TOKEN_NAME) is None
    assert store.read_secret_bytes("smktok-abc.json") is None


def test_logout_when_signed_out_is_noop(store, priv, capsys):
    rc = priv.cmd_logout([])
    assert rc == 0
    assert _last_json(capsys)["removed"] == []


# --- revoke ---------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def _patch_post(monkeypatch, resp=None, raises=None):
    calls = {}
    import requests

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_revoke_200_revokes_and_removes_local(store, priv, monkeypatch, capsys):
    _seed_token(store)
    calls = _patch_post(monkeypatch, resp=_FakeResp(200))
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 0
    out = _last_json(capsys)
    assert out["status"] == "revoked" and out["revoked"] is True
    assert store.read_secret_bytes(store.TOKEN_NAME) is None
    # the refresh token travels only in the POST form body, never the URL
    assert calls["url"] == priv.REVOKE_URI
    assert "RT" not in calls["url"]
    assert calls["kwargs"]["data"] == {"token": "RT"}
    assert calls["kwargs"]["allow_redirects"] is False


def test_revoke_invalid_token_is_success(store, priv, monkeypatch, capsys):
    _seed_token(store)
    _patch_post(monkeypatch, resp=_FakeResp(400, {"error": "invalid_token"}))
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 0
    assert _last_json(capsys)["status"] == "revoked"
    assert store.read_secret_bytes(store.TOKEN_NAME) is None


def test_revoke_invalid_request_keeps_token(store, priv, monkeypatch, capsys):
    _seed_token(store)
    _patch_post(monkeypatch, resp=_FakeResp(400, {"error": "invalid_request"}))
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 1
    out = _last_json(capsys)
    assert out["status"] == "request_error" and out["token_kept"] is True
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


def test_revoke_5xx_is_ambiguous_keeps_token(store, priv, monkeypatch, capsys):
    _seed_token(store)
    _patch_post(monkeypatch, resp=_FakeResp(503))
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 1
    out = _last_json(capsys)
    assert out["status"] == "ambiguous" and out["token_kept"] is True
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


def test_revoke_network_error_is_ambiguous_keeps_token(store, priv, monkeypatch,
                                                       capsys):
    _seed_token(store)
    import requests
    _patch_post(monkeypatch,
                raises=requests.exceptions.ConnectTimeout("boom"))
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 1
    assert _last_json(capsys)["status"] == "ambiguous"
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


def test_revoke_non_tty_without_yes_refuses_and_makes_no_call(
        store, priv, monkeypatch, capsys):
    _seed_token(store)
    _no_tty(monkeypatch)
    calls = _patch_post(monkeypatch, resp=_FakeResp(200))
    rc = priv.cmd_revoke([])
    assert rc == 2
    assert _last_json(capsys)["status"] == "needs_confirmation"
    assert calls == {}  # never contacted Google
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


def test_revoke_nothing_to_revoke(store, priv, capsys):
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 0
    assert _last_json(capsys)["status"] == "nothing_to_revoke"


def test_revoke_success_keeps_concurrently_reauthed_token(store, priv,
                                                          monkeypatch, capsys):
    _seed_token(store, refresh="RT_A")
    import requests

    def fake_post(url, **kwargs):
        # simulate a concurrent `init --reauth` installing a NEW token while the
        # network revoke of RT_A is in flight
        store.write_token_envelope({"refresh_token": "RT_B", "token": "AT_B"},
                                   {"granted_scopes": ["s"]})
        return _FakeResp(200)

    monkeypatch.setattr(requests, "post", fake_post)
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 0
    out = _last_json(capsys)
    assert out["status"] == "revoked_superseded_locally"
    assert out["token_kept"] is True
    # the NEWER token B must still be on disk — we revoked A, not B
    env = store.read_token_envelope()
    assert env["token"]["refresh_token"] == "RT_B"


def test_revoke_success_but_local_cleanup_fails(store, priv, monkeypatch,
                                                capsys):
    _seed_token(store)
    _patch_post(monkeypatch, resp=_FakeResp(200))
    real_remove = store.remove_secret

    def flaky(name):
        if name == store.TOKEN_NAME:
            raise OSError("cannot unlink")
        return real_remove(name)

    monkeypatch.setattr(store, "remove_secret", flaky)
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 1
    out = _last_json(capsys)
    assert out["status"] == "revoked_local_cleanup_failed"
    assert out["revoked"] is True and out["token_kept"] is True
    assert store.TOKEN_NAME in out["failed"]


def test_revoke_keeps_token_when_store_unreadable(store, priv, monkeypatch,
                                                  capsys):
    # if the locked re-read fails we cannot prove which token is on disk —
    # fail closed and keep it, never delete blindly (codex r3-priv #P1)
    _seed_token(store)
    _patch_post(monkeypatch, resp=_FakeResp(200))
    real_read = store.read_token_envelope
    calls = {"n": 0}

    def flaky_read():
        calls["n"] += 1
        if calls["n"] >= 2:  # the re-read inside _cleanup_after_revoke
            raise store.ConfigError("corrupt store")
        return real_read()

    monkeypatch.setattr(store, "read_token_envelope", flaky_read)
    rc = priv.cmd_revoke(["--yes"])
    assert rc == 1
    out = _last_json(capsys)
    assert out["status"] == "revoked_local_cleanup_failed"
    assert out["token_kept"] is True
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


# --- forget ---------------------------------------------------------------

def _seed_all(store):
    _seed_token(store)
    store.write_secret_bytes(store.CREDENTIALS_NAME, b'{"installed":{}}')
    store.write_secret_bytes("smoke-abc.json", b'{"state":"x"}')
    store.write_secret_bytes("smktok-abc.json", b'{"token":{}}')
    import skrepka.setup as setup
    store.write_secret_bytes(setup.RECOVERY_CRED, b'{}')
    store.write_secret_bytes(setup.RECOVERY_TOKEN, b'{}')


def test_forget_dry_run_removes_nothing(store, priv, capsys):
    _seed_all(store)
    rc = priv.cmd_forget(["--dry-run"])
    assert rc == 0
    out = _last_json(capsys)
    assert out["dry_run"] is True
    assert store.TOKEN_NAME in out["would_remove"]
    assert out["external_copies_not_controlled"]
    # nothing actually deleted
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None
    assert store.read_secret_bytes(store.CREDENTIALS_NAME) is not None


def test_forget_yes_removes_all_but_keeps_lock_and_dir(store, priv, capsys):
    _seed_all(store)
    with store.lock():  # materialize the .lock file
        pass
    lock_path = os.path.join(store.config_dir(), ".lock")
    assert os.path.exists(lock_path)

    rc = priv.cmd_forget(["--yes"])
    assert rc == 0
    for name in (store.TOKEN_NAME, store.CREDENTIALS_NAME, "smoke-abc.json",
                 "smktok-abc.json"):
        assert store.read_secret_bytes(name) is None
    # the lock file and the config dir itself survive
    assert os.path.exists(lock_path)
    assert os.path.isdir(store.config_dir())


def test_forget_non_tty_without_yes_refuses(store, priv, monkeypatch, capsys):
    _seed_all(store)
    _no_tty(monkeypatch)
    rc = priv.cmd_forget([])
    assert rc == 2
    assert _last_json(capsys)["status"] == "needs_confirmation"
    assert store.read_secret_bytes(store.TOKEN_NAME) is not None


CRASH_TEMP = ".token.json.0123456789abcdef.tmp"  # .<name>.<16 hex>.tmp


def test_forget_removes_crash_temps_but_keeps_unrelated(store, priv, capsys):
    _seed_token(store)
    # a half-written secret temp left by a SIGKILL mid atomic-write
    store.write_secret_bytes(CRASH_TEMP, b"HALF_SECRET")
    # an unrelated dotfile of the same suffix must NOT match the crash shape
    store.write_secret_bytes(".unrelated.tmp", b"keep me")
    rc = priv.cmd_forget(["--yes"])
    assert rc == 0
    out = _last_json(capsys)
    assert CRASH_TEMP in out["local_data_removed"]
    assert ".unrelated.tmp" not in out["local_data_removed"]
    assert store.read_secret_bytes(CRASH_TEMP) is None
    assert store.read_secret_bytes(".unrelated.tmp") is not None


def test_forget_dry_run_lists_crash_temp(store, priv, capsys):
    store.write_secret_bytes(CRASH_TEMP, b"HALF_SECRET")
    rc = priv.cmd_forget(["--dry-run"])
    assert rc == 0
    assert CRASH_TEMP in _last_json(capsys)["would_remove"]
    # dry-run must not delete it
    assert store.read_secret_bytes(CRASH_TEMP) is not None


def test_forget_sidecars_removes_sidecar_not_md(store, priv, tmp_path, capsys):
    md = tmp_path / "doc.md"
    md.write_text("# doc")
    sidecar = tmp_path / ("doc.md" + store.SIDECAR_SUFFIX)
    sidecar.write_text("{}")

    rc = priv.cmd_forget(["--yes", "--sidecars", str(md)])
    assert rc == 0
    out = _last_json(capsys)
    assert str(sidecar) in out["local_data_removed"]
    assert not sidecar.exists()
    assert md.exists()  # the user's own markdown is never deleted
