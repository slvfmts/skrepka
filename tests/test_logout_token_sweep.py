"""logout / revoke must remove EVERY local artifact that can still sign in.

The list used to live in three hand-written copies, and `recovery.token.json`
plus the atomic-write crash temps only ever made it into `forget`. After an
interrupted `init --reauth` those hold a working refresh token, so logout
reported success while the agent could still reach Google.

Two invariants that are easy to state and were previously untested:
  * the OAuth client (and its temps) survives — logout promises to keep it;
  * the write-ahead marker is a barrier, so it goes only if every
    token-bearing file went first.
"""

import os

import pytest

from skrepka import config, privacy, setup


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = home / ".config" / "skrepka"
    d.mkdir(parents=True)
    for p in (home, home / ".config", d):
        os.chmod(p, 0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "config_dir", lambda: str(d))
    monkeypatch.setattr(privacy, "_emit", lambda payload: None)
    return d


def _seed(d):
    """Every shape of on-disk artifact, including ones only forget knew."""
    names = [
        config.TOKEN_NAME,
        config.CREDENTIALS_NAME,
        config.MARKER_NAME,
        setup.RECOVERY_TOKEN,
        setup.RECOVERY_CRED,
        f"{setup.TOKEN_PREFIX}abc.json",                 # smoke token
        f".{config.TOKEN_NAME}.0123456789abcdef.tmp",    # token crash temp
        f".{setup.TOKEN_PREFIX}abc.json.0123456789abcdef.tmp",
        f".{config.CREDENTIALS_NAME}.0123456789abcdef.tmp",  # client temp
    ]
    for n in names:
        (d / n).write_bytes(b"x")
        os.chmod(d / n, 0o600)
    return names


def test_logout_removes_every_usable_token(store):
    _seed(store)
    privacy.cmd_logout([])
    for gone in (config.TOKEN_NAME, setup.RECOVERY_TOKEN,
                 f"{setup.TOKEN_PREFIX}abc.json",
                 f".{config.TOKEN_NAME}.0123456789abcdef.tmp",
                 f".{setup.TOKEN_PREFIX}abc.json.0123456789abcdef.tmp"):
        assert not (store / gone).exists(), f"{gone} still usable after logout"


def test_logout_keeps_the_oauth_client_and_its_temp(store):
    _seed(store)
    privacy.cmd_logout([])
    assert (store / config.CREDENTIALS_NAME).exists()
    assert (store / setup.RECOVERY_CRED).exists()
    # a crash temp of the CLIENT is not token-bearing; sweeping it would
    # break logout's promise to keep the client
    assert (store / f".{config.CREDENTIALS_NAME}.0123456789abcdef.tmp").exists()


def test_logout_removes_the_marker_when_everything_went(store):
    _seed(store)
    privacy.cmd_logout([])
    assert not (store / config.MARKER_NAME).exists()


def test_marker_survives_when_a_token_delete_fails(store, monkeypatch):
    """The marker makes readers fail closed. If a usable token is still on
    disk, tearing the barrier down is the worst of both worlds."""
    _seed(store)
    real = config.remove_secret

    def flaky(name):
        if name == setup.RECOVERY_TOKEN:
            raise OSError("EPERM")
        return real(name)

    monkeypatch.setattr(config, "remove_secret", flaky)
    privacy.cmd_logout([])
    assert (store / setup.RECOVERY_TOKEN).exists(), "precondition"
    assert (store / config.MARKER_NAME).exists(), \
        "barrier removed while a usable token remained"


def test_forget_targets_still_cover_the_client_side(store):
    _seed(store)
    targets = privacy._forget_targets()
    for n in (config.TOKEN_NAME, config.CREDENTIALS_NAME,
              setup.RECOVERY_TOKEN, setup.RECOVERY_CRED,
              config.MARKER_NAME):
        assert n in targets, f"forget no longer removes {n}"
    assert len(targets) == len(set(targets)), "duplicate targets"


def test_revoke_cleanup_fails_closed_on_an_unreadable_store(store):
    """If the envelope cannot be read we cannot prove which token is on disk,
    so nothing is deleted — a concurrent `init --reauth` may have installed a
    newer sign-in that must not be destroyed."""
    (store / config.TOKEN_NAME).write_bytes(b"not json")
    res = privacy._cleanup_after_revoke("somehash")
    assert res["verifiable"] is False
    assert res["removed"] == []
    assert res["token_present"] is True
    assert (store / config.TOKEN_NAME).exists()
