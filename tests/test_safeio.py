"""Hardened artifact I/O (r3 #9): a downloaded doc / sidecar / --output file
must never be written THROUGH a symlink planted at the target or any parent
component, and must land atomically with 0600. These tests are the security
core — if one fails, the symlink-overwrite attack is open again."""

import os
import stat

import pytest

from skrepka import safeio


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


# --- happy path ---

def test_atomic_write_roundtrip_str_and_bytes(tmp_path):
    p = tmp_path / "out.md"
    ret = safeio.atomic_write(str(p), "héllo\n")
    assert ret == str(p)
    assert p.read_text() == "héllo\n"
    assert _mode(str(p)) == 0o600

    q = tmp_path / "out.bin"
    safeio.atomic_write(str(q), b"\x00\x01\x02")
    assert q.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_replaces_existing_regular(tmp_path):
    p = tmp_path / "out.md"
    p.write_text("old")
    safeio.atomic_write(str(p), "new")
    assert p.read_text() == "new"


def test_no_temp_left_behind(tmp_path):
    p = tmp_path / "out.md"
    safeio.atomic_write(str(p), "x")
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


# --- the attack: symlinked final target must NOT be written through ---

def test_refuses_symlinked_target_and_leaves_victim_untouched(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    link = tmp_path / "out.md"
    link.symlink_to(victim)

    with pytest.raises(safeio.SafeIOError):
        safeio.atomic_write(str(link), "PWNED")

    # the symlink target must be byte-for-byte unchanged
    assert victim.read_text() == "SECRET"


def test_refuses_symlinked_parent_component(tmp_path):
    real = tmp_path / "realdir"
    real.mkdir()
    linkdir = tmp_path / "linkdir"
    linkdir.symlink_to(real)

    with pytest.raises(safeio.SafeIOError):
        safeio.atomic_write(str(linkdir / "file.txt"), "x")
    # nothing was written into the real directory
    assert list(real.iterdir()) == []


def test_refuses_non_regular_target_directory(tmp_path):
    d = tmp_path / "target"
    d.mkdir()
    with pytest.raises(safeio.SafeIOError):
        safeio.atomic_write(str(d), "x")


def test_missing_dir_without_create_fails_closed(tmp_path):
    with pytest.raises(safeio.SafeIOError):
        safeio.atomic_write(str(tmp_path / "nope" / "file.txt"), "x")


# --- directory creation (immediate parent only) ---

def test_make_parents_creates_immediate_parent_0700(tmp_path):
    target = tmp_path / "newdir" / "file.txt"
    safeio.atomic_write(str(target), "x", make_parents=True)
    assert target.read_text() == "x"
    assert _mode(str(tmp_path / "newdir")) == 0o700


def test_make_parents_creates_nested_chain_0700(tmp_path):
    # a missing multi-level chain must be created (regression: safeio once made
    # only the immediate parent, breaking --images-dir a/b/c — codex r3-io #P2)
    target = tmp_path / "a" / "b" / "c" / "file.txt"
    safeio.atomic_write(str(target), "x", make_parents=True)
    assert target.read_text() == "x"
    for p in (tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "c"):
        assert _mode(str(p)) == 0o700


def test_atomic_write_over_hardlink_does_not_truncate_victim(tmp_path):
    # a planted HARD LINK at the target must not have its shared inode truncated
    # (O_NOFOLLOW does not catch hard links; atomic rename replaces the name)
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    link = tmp_path / "out.tmp"
    os.link(str(victim), str(link))  # hard link: same inode
    safeio.atomic_write(str(link), "NEW")
    assert link.read_text() == "NEW"
    assert victim.read_text() == "SECRET"  # victim inode untouched


def test_make_parents_refuses_symlinked_immediate_parent(tmp_path):
    # the immediate parent carries a predictable name (e.g. <doc>_images) — a
    # symlink planted there must fail closed even when create=True
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(safeio.SafeIOError):
        safeio.atomic_write(str(link / "f.txt"), "x", make_parents=True)
    assert list(real.iterdir()) == []


def test_symlinked_ancestor_is_followed_but_leaf_stays_strict(tmp_path):
    # ANCESTORS above the immediate parent are the user's environment and are
    # followed (mandatory on macOS where /var etc. are symlinks). The immediate
    # parent we create underneath is still O_NOFOLLOW/exclusive.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"          # a symlinked ancestor
    link.symlink_to(real)
    target = link / "leaf" / "file.txt"   # 'leaf' is created under the ancestor
    safeio.atomic_write(str(target), "ok", make_parents=True)
    # resolves through the symlinked ancestor into the real tree
    assert (real / "leaf" / "file.txt").read_text() == "ok"
    assert _mode(str(real / "leaf")) == 0o700


# --- write_at guards ---

def test_write_at_rejects_slash_in_name(tmp_path):
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(safeio.SafeIOError):
            safeio.write_at(fd, "a/b", b"x")
    finally:
        os.close(fd)


def test_write_at_refuses_symlinked_target_in_dir(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    (tmp_path / "link").symlink_to(victim)
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(safeio.SafeIOError):
            safeio.write_at(fd, "link", b"PWNED")
    finally:
        os.close(fd)
    assert victim.read_text() == "SECRET"


def test_refuses_filesystem_root():
    with pytest.raises(safeio.SafeIOError):
        safeio.secure_open_parent("/")


# --- engine call sites actually use the hardened path ---

def test_emit_json_refuses_symlinked_output(tmp_path):
    import skrepka._engine as engine
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    (tmp_path / "out.json").symlink_to(victim)
    with pytest.raises(safeio.SafeIOError):
        engine._emit_json({"a": 1}, output=str(tmp_path / "out.json"))
    assert victim.read_text() == "SECRET"


def test_write_sidecar_refuses_symlink_and_is_atomic(tmp_path):
    import skrepka._engine as engine
    md = tmp_path / "doc.md"
    victim = tmp_path / "victim"
    victim.write_text("SECRET")
    # the sidecar path is md + SIDECAR_SUFFIX — plant a symlink there
    os.symlink(str(victim), str(md) + engine.SIDECAR_SUFFIX)
    with pytest.raises(safeio.SafeIOError):
        engine._write_sidecar(str(md), {"schema_version": 2})
    assert victim.read_text() == "SECRET"


def test_write_sidecar_roundtrip(tmp_path):
    import skrepka._engine as engine
    md = tmp_path / "doc.md"
    path = engine._write_sidecar(str(md), {"schema_version": 2})
    assert path == str(md) + engine.SIDECAR_SUFFIX
    assert '"schema_version"' in open(path).read()
