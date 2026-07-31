"""Human-gate for human-only operations + --output receipt emission."""

import json

import pytest


# --- _require_human ---

def test_gate_blocks_non_interactive(engine, monkeypatch, capsys):
    monkeypatch.delenv("SKREPKA_ASSUME_HUMAN", raising=False)
    # pytest runs with stdin/stderr not attached to a TTY — exactly the
    # agent situation the gate exists for
    with pytest.raises(SystemExit) as exc:
        engine._require_human("resolve comment thread")
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)
    assert "human-only" in err["error"]
    assert "SKREPKA_ASSUME_HUMAN" in err["error"]


def test_gate_env_override_passes(engine, monkeypatch):
    monkeypatch.setenv("SKREPKA_ASSUME_HUMAN", "1")
    engine._require_human("resolve comment thread")  # must not raise


def test_gate_env_other_values_do_not_pass(engine, monkeypatch):
    monkeypatch.setenv("SKREPKA_ASSUME_HUMAN", "true")
    with pytest.raises(SystemExit):
        engine._require_human("resolve comment thread")


def test_gate_tty_confirmation_yes(engine, monkeypatch):
    monkeypatch.delenv("SKREPKA_ASSUME_HUMAN", raising=False)

    class FakeTTY:
        def isatty(self):
            return True

        def readline(self):
            return "y\n"

    monkeypatch.setattr(engine.sys, "stdin", FakeTTY())
    monkeypatch.setattr(engine.sys, "stderr", __import__("io").StringIO())
    monkeypatch.setattr(engine.sys.stderr, "isatty", lambda: True,
                        raising=False)
    engine._require_human("update --acknowledge-loss")  # confirmed


def test_gate_tty_confirmation_default_is_no(engine, monkeypatch, capsys):
    monkeypatch.delenv("SKREPKA_ASSUME_HUMAN", raising=False)

    class FakeTTY:
        def isatty(self):
            return True

        def readline(self):
            return "\n"  # bare Enter must NOT confirm

    import io
    fake_err = io.StringIO()
    fake_err.isatty = lambda: True
    monkeypatch.setattr(engine.sys, "stdin", FakeTTY())
    monkeypatch.setattr(engine.sys, "stderr", fake_err)
    with pytest.raises(SystemExit):
        engine._require_human("resolve comment thread")
    assert "not confirmed" in capsys.readouterr().out


# --- _emit_json ---

def test_emit_json_prints_by_default(engine, capsys):
    engine._emit_json({"a": "б"})
    assert json.loads(capsys.readouterr().out) == {"a": "б"}


def test_emit_json_writes_file_and_prints_receipt(engine, tmp_path, capsys):
    target = tmp_path / "comments.json"
    payload = [{"id": "c1", "content": "х" * 100}]
    engine._emit_json(payload, output=str(target),
                      summary={"comments": 1})
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == payload
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["written"] == str(target)
    assert receipt["comments"] == 1
    assert receipt["bytes"] == target.stat().st_size
    # the receipt itself must stay small — that is its whole point
    assert "content" not in receipt


# --- list_comments field selection (#16) ---

def test_list_comments_asks_for_created_time_and_reply_ids(engine, monkeypatch,
                                                           capsys):
    """Without these the refusal on colliding accounting keys names threads by
    a second the person cannot look up anywhere, and the surplus reply has no
    id to delete by (#18)."""
    seen = {}

    class _Req:
        def execute(self):
            return {"comments": [{"id": "c1", "content": "x"}]}

    class Drive:
        def comments(self):
            return self

        def list(self, **kw):
            seen.update(kw)
            return _Req()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: Drive())
    engine.list_comments("doc1")
    capsys.readouterr()

    fields = seen["fields"]
    assert "replies(id,content,author/displayName,createdTime)" in fields
    # the parent's own createdTime, not just the replies' one
    assert "comments(id,content,author/displayName,createdTime," in fields


# --- _write_control fail-closed hardening (codex delta review) ---

def test_write_control_refuses_missing_revision(engine, capsys):
    # the google client drops None from request bodies: an unvalidated None
    # would silently turn a pinned write into an UNPINNED one
    with pytest.raises(SystemExit):
        engine._write_control(None)
    assert "unpinned" in json.loads(capsys.readouterr().out)["error"]


def test_write_control_pins_revision(engine):
    assert engine._write_control("rev7") == {"requiredRevisionId": "rev7"}
