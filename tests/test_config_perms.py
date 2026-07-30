"""Permissions and lock hardening in the config store.

Two rules run through these tests:

  * what is OURS and merely loose gets repaired, because refusing would lock
    the owner out of their own store — and for `logout`/`forget` a refusal
    would *extend* a secret's life instead of ending it;
  * what is not ours, or writable by others, is refused, because chmod does
    not revoke a descriptor someone already holds open and the contents can
    no longer be trusted.
"""

import os
import stat

import pytest

from skrepka import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A config dir under a fake HOME so _check_dir_chain is satisfied."""
    home = tmp_path / "home"
    d = home / ".config" / "skrepka"
    d.mkdir(parents=True)
    os.chmod(home, 0o700)
    os.chmod(home / ".config", 0o700)
    os.chmod(d, 0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "config_dir", lambda: str(d))
    return d


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_dir_0755_is_tightened(store):
    os.chmod(store, 0o755)
    config.ensure_config_dir()
    assert _mode(store) == 0o700


def test_dir_0750_is_tightened(store):
    """Group-readable is exposure too, not only group-writable."""
    os.chmod(store, 0o750)
    config.ensure_config_dir()
    assert _mode(store) == 0o700


def test_readable_secret_is_repaired_not_refused(store):
    p = store / config.TOKEN_NAME
    p.write_bytes(b'{"token": {}}')
    os.chmod(p, 0o644)
    data = config.read_secret_bytes(config.TOKEN_NAME)
    assert data == b'{"token": {}}'
    assert _mode(p) == 0o600


def test_group_writable_secret_is_refused(store):
    """Integrity, not confidentiality: the contents may have been replaced."""
    p = store / config.TOKEN_NAME
    p.write_bytes(b'{"token": {}}')
    os.chmod(p, 0o660)
    with pytest.raises(config.ConfigError) as e:
        config.read_secret_bytes(config.TOKEN_NAME)
    assert "writable by other users" in str(e.value)


def test_world_writable_secret_is_refused(store):
    p = store / config.TOKEN_NAME
    p.write_bytes(b'{"token": {}}')
    os.chmod(p, 0o666)
    with pytest.raises(config.ConfigError):
        config.read_secret_bytes(config.TOKEN_NAME)


def test_lock_works_and_tightens_a_loose_lock(store):
    lock_path = store / ".lock"
    lock_path.write_bytes(b"")
    os.chmod(lock_path, 0o666)
    with config.lock():
        pass
    # a lock others can write is a DoS handle; ours to repair
    assert _mode(lock_path) == 0o600


def test_lock_refuses_a_symlinked_lock(store, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    os.symlink(target, store / ".lock")
    with pytest.raises(config.ConfigError) as e:
        with config.lock():
            pass
    assert "symlink" in str(e.value)


def test_lock_refuses_a_directory_in_place_of_the_lock(store):
    (store / ".lock").mkdir()
    with pytest.raises(config.ConfigError):
        with config.lock():
            pass


def test_lock_validates_the_dir_chain(store, monkeypatch):
    """logout/forget take the lock before any protected read — the chain must
    already have been checked by then."""
    calls = []
    real = config._check_dir_chain
    monkeypatch.setattr(config, "_check_dir_chain",
                        lambda p: (calls.append(p), real(p))[1])
    with config.lock():
        pass
    assert calls, "lock() must validate the parent chain"
