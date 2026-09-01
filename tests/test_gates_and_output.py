"""Consent gate for the person's own decisions + --output receipt emission.

Until 0.9.2 the gate demanded a live TTY. A process launched by an agent has
none (measured: no TTY on stdin/stderr, no /dev/tty), so the legitimate human
path was unreachable and the only way through was a person pasting a command
an agent composed. The flag is now the confirmation (#17); these tests pin
both halves of that: the default path still refuses, and the terminal is no
longer required.
"""

import io
import json
import sys

import pytest

REMEDY = "Ask the person to close the thread in the Google Docs UI."


class _Exploded(Exception):
    """Raised by a stub that must never be reached."""


def _fake_tty(monkeypatch, engine, answer):
    """Attach a fake interactive terminal answering `answer`."""
    class FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            return answer

    fake_err = io.StringIO()
    fake_err.isatty = lambda: True
    monkeypatch.setattr(engine.sys, "stdin", FakeStdin())
    monkeypatch.setattr(engine.sys, "stderr", fake_err)


def _explode_on_auth(monkeypatch, engine):
    """Make any API path blow up, so a test that claims 'refused before the
    API' cannot pass on an unrelated 'not signed in' error."""
    def _boom():
        raise _Exploded("get_creds must not be reached")
    monkeypatch.setattr(engine, "get_creds", _boom)


# --- _require_consent ---

def test_consent_refuses_without_flag_non_interactive(engine, capsys):
    # pytest runs with stdin/stderr not attached to a TTY — exactly the agent
    # situation the gate exists for
    with pytest.raises(SystemExit) as exc:
        engine._require_consent("resolve comment thread", False, REMEDY)
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert "--yes" in err
    assert REMEDY in err
    # the removed bypass must not come back, and the refusal must not hand a
    # command to the person to paste (both are the whole point of #17)
    assert "SKREPKA_ASSUME_HUMAN" not in err
    assert "themselves" not in err


def test_consent_refusal_does_not_end_on_the_flag(engine, capsys):
    """Measured in the acceptance run: with "rerun with --yes" as the closing
    sentence, an agent with no contract in context read it as its own next
    step and passed the flag. The prohibition has to come first and the flag
    must never be the last thing said."""
    with pytest.raises(SystemExit):
        engine._require_consent("resolve comment thread", False, REMEDY)
    err = json.loads(capsys.readouterr().out)["error"]
    assert err.index("not the agent's") < err.index("--yes")
    assert not err.rstrip().endswith("rerun with --yes.")


def test_consent_flag_alone_passes_without_terminal(engine):
    engine._require_consent("resolve comment thread", True, REMEDY)


def test_consent_env_var_is_no_longer_honoured(engine, monkeypatch):
    """The old bypass is gone, not renamed: setting it changes nothing."""
    monkeypatch.setenv("SKREPKA_ASSUME_HUMAN", "1")
    with pytest.raises(SystemExit):
        engine._require_consent("resolve comment thread", False, REMEDY)


def test_consent_tty_confirmation_yes(engine, monkeypatch):
    _fake_tty(monkeypatch, engine, "y\n")
    engine._require_consent("resolve comment thread", False, REMEDY)


def test_consent_tty_confirmation_default_is_no(engine, monkeypatch, capsys):
    _fake_tty(monkeypatch, engine, "\n")  # bare Enter must NOT confirm
    with pytest.raises(SystemExit):
        engine._require_consent("resolve comment thread", False, REMEDY)
    assert "not confirmed" in capsys.readouterr().out


def test_consent_treats_a_missing_stdin_as_no_terminal(engine, monkeypatch,
                                                       capsys):
    """pythonw, a closed fd, a detached service: sys.stdin can be None. That
    must refuse with the usual JSON, not raise AttributeError inside a gate."""
    monkeypatch.setattr(engine.sys, "stdin", None)
    with pytest.raises(SystemExit):
        engine._require_consent("resolve comment thread", False, REMEDY)
    assert "--yes" in json.loads(capsys.readouterr().out)["error"]


def test_consent_refuses_when_the_terminal_cannot_be_read(engine, monkeypatch,
                                                          capsys):
    class BrokenStdin:
        def isatty(self):
            return True

        def readline(self):
            raise OSError("terminal went away")

    fake_err = io.StringIO()
    fake_err.isatty = lambda: True
    monkeypatch.setattr(engine.sys, "stdin", BrokenStdin())
    monkeypatch.setattr(engine.sys, "stderr", fake_err)
    with pytest.raises(SystemExit):
        engine._require_consent("resolve comment thread", False, REMEDY)
    assert "not confirmed" in capsys.readouterr().out


# --- resolve / reply --resolve wiring ---

def _fake_reply_drive(seen, comments=()):
    """Дублёр Drive для одиночного `reply`.

    С T6 у `reply` есть секундный шлюз, и он читает перепись перед записью.
    Пустая перепись — законный случай «столкнуться не с чем»: паузы нет, и
    тесты ниже это заодно доказывают. Не умей дублёр `comments()`, шлюз
    честно ждал бы вслепую, и каждый такой тест стоил бы секунды.
    """
    class _Req:
        def __init__(self, payload=None):
            self.payload = payload

        def execute(self):
            return self.payload or {
                "id": "r1", "content": "ok", "action": "resolve",
                "createdTime": "2026-07-31T10:00:00.000Z",
                "author": {"displayName": "Slava"}}

    class Drive:
        def replies(self):
            return self

        def comments(self):
            return self

        def list(self, **kw):
            return _Req({"comments": [dict(c) for c in comments]})

        def about(self):
            return self

        def get(self, **kw):
            return _Req({"user": {"displayName": "Slava",
                                  "permissionId": "pid-1"}})

        def create(self, **kw):
            seen.update(kw)
            return _Req()
    return Drive()


def test_resolve_without_flag_refuses_before_any_api_call(engine, monkeypatch,
                                                          capsys):
    _explode_on_auth(monkeypatch, engine)
    with pytest.raises(SystemExit):
        engine.resolve_comment("doc1", "c1")
    assert "--yes" in json.loads(capsys.readouterr().out)["error"]


def test_reply_resolve_without_flag_refuses_before_any_api_call(
        engine, monkeypatch, capsys):
    _explode_on_auth(monkeypatch, engine)
    with pytest.raises(SystemExit) as exc:
        engine.reply_comment("doc1", "c1", "text", resolve=True)
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)["error"]
    # the refusal has to carry the remedy the manual checklist looks for
    assert "Google Docs UI" in err
    assert "--yes" in err


def test_resolve_with_flag_reaches_the_api(engine, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_reply_drive(seen))
    engine.resolve_comment("doc1", "c1", yes=True)
    assert seen["body"]["action"] == "resolve"
    assert json.loads(capsys.readouterr().out)["resolved"] is True


def test_cli_passes_the_flag_through_to_resolve(engine, monkeypatch, capsys):
    """A unit test of the helper cannot prove main() threads args.yes; without
    this, a forgotten `yes=args.yes` ships a resolve that always refuses."""
    seen = {}
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_reply_drive(seen))
    monkeypatch.setattr(sys, "argv", ["skrepka", "resolve", "doc1", "c1",
                                      "--yes"])
    engine.main()
    assert seen["body"]["action"] == "resolve"
    capsys.readouterr()


def test_cli_passes_the_flag_through_to_reply_resolve(engine, monkeypatch,
                                                      capsys):
    """The `reply --resolve` branch of the dispatcher threads its own copy of
    args.yes; without this the resolve-only test would let a dropped
    `yes=args.yes` here ship a path that always refuses."""
    seen = {}
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_reply_drive(seen))
    monkeypatch.setattr(sys, "argv", ["skrepka", "reply", "doc1", "c1", "hi",
                                      "--resolve", "--yes"])
    engine.main()
    assert seen["body"]["action"] == "resolve"
    capsys.readouterr()


def test_cli_resolve_without_flag_still_refuses(engine, monkeypatch, capsys):
    _explode_on_auth(monkeypatch, engine)
    monkeypatch.setattr(sys, "argv", ["skrepka", "resolve", "doc1", "c1"])
    with pytest.raises(SystemExit):
        engine.main()
    assert "--yes" in json.loads(capsys.readouterr().out)["error"]


def test_reply_yes_without_resolve_is_a_usage_error(engine, monkeypatch,
                                                     capsys):
    """A silently ignored --yes teaches an agent to hang it on everything."""
    _explode_on_auth(monkeypatch, engine)
    monkeypatch.setattr(sys, "argv", ["skrepka", "reply", "doc1", "c1", "hi",
                                      "--yes"])
    with pytest.raises(SystemExit):
        engine.main()
    assert "--resolve" in json.loads(capsys.readouterr().out)["error"]


def test_reply_prints_the_second_its_reply_landed_in(engine, monkeypatch,
                                                      capsys):
    """Anchor accounting keys on (author, second); without this field an agent
    cannot see the second its own reply took (#15/#16)."""
    seen = {}
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_reply_drive(seen))
    engine.reply_comment("doc1", "c1", "text")
    out = json.loads(capsys.readouterr().out)
    assert out["createdTime"] == "2026-07-31T10:00:00.000Z"


# --- update: the flag is the confirmation, no terminal involved ---

def _fake_update_drive(calls, *, let_it_run=False):
    """let_it_run=False stops at the first write, so a test can prove the gate
    let the operation through without actually walking the whole path."""
    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class Files:
        def get(self, **kw):
            return _Resp({"id": "doc1", "name": "doc", "parents": ["folder1"],
                          "webViewLink": "https://example/doc1"})

        def copy(self, **kw):
            calls.append("copy")
            if not let_it_run:
                raise _Exploded("backup reached")
            return _Resp({"id": "backup1", "name": "doc.pre-update-backup-x",
                          "parents": ["folder1"]})

        def update(self, **kw):
            calls.append("update")
            if not let_it_run:
                raise _Exploded("destructive write reached")
            return _Resp({})

    class Drive:
        def files(self):
            return Files()
    return Drive()


def _stub_update_preflight(monkeypatch, engine, comments, named_ranges=()):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda c: object())
    monkeypatch.setattr(engine, "_census_comments",
                        lambda d, f: (comments, comments, "fp", {}))
    monkeypatch.setattr(engine, "_safe_get_doc",
                        lambda d, f: {"namedRanges": {n: {} for n
                                                      in named_ranges}})


def test_update_without_acknowledge_loss_blocks_before_any_write(
        engine, monkeypatch, tmp_path, capsys):
    calls = []
    _stub_update_preflight(monkeypatch, engine, [{"id": "c1"}])
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive(calls))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.update_doc("doc1", str(md))
    assert exc.value.code == 2
    assert calls == []  # neither the backup nor the replace happened
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["comments"] == 1
    # the agent has to be able to name the document it is asking about
    assert blocked["document"] == "doc"
    # asking comes before the flag, and consent does not travel between docs
    reason = blocked["reason"]
    assert reason.index("ask the person") < reason.index("--acknowledge-loss")
    assert "does not carry over" in reason


def test_update_blocks_on_named_ranges_alone(engine, monkeypatch, tmp_path,
                                             capsys):
    """A doc with no comments but with named ranges is the other half of the
    guard — `mark` puts them there and a full replace destroys them."""
    calls = []
    _stub_update_preflight(monkeypatch, engine, [], named_ranges=("intro",))
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive(calls))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.update_doc("doc1", str(md))
    assert exc.value.code == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out)["named_ranges"] == ["intro"]


def test_update_with_acknowledge_loss_needs_no_terminal(
        engine, monkeypatch, tmp_path):
    """The regression #17 fixes: with the flag, a non-interactive run must get
    all the way to the backup instead of being stopped by a missing TTY."""
    calls = []
    _stub_update_preflight(monkeypatch, engine, [{"id": "c1"}])
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive(calls))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    with pytest.raises(_Exploded):
        engine.update_doc("doc1", str(md), acknowledge_loss=True)
    assert calls == ["copy"]  # backup attempted, no write yet


def test_destructive_receipt_says_the_consent_was_one_time(
        engine, monkeypatch, tmp_path, capsys):
    """The refusal is the only place the rule is stated, and an agent that
    already knows the flag never sees it again — that is how a second document
    got destroyed on no consent at all. The receipt has to repeat it."""
    calls = []
    _stub_update_preflight(monkeypatch, engine, [{"id": "c1"}])
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive(calls, let_it_run=True))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    engine.update_doc("doc1", str(md), acknowledge_loss=True)
    out = json.loads(capsys.readouterr().out)
    assert calls == ["copy", "update"]
    assert "one-time consent" in out["consent_note"]
    assert "any other document" in out["consent_note"]


def test_clean_document_receipt_has_no_consent_note(
        engine, monkeypatch, tmp_path, capsys):
    """No comments, no named ranges: nothing was destroyed and no consent was
    spent, so the note would be noise."""
    calls = []
    _stub_update_preflight(monkeypatch, engine, [])
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive(calls, let_it_run=True))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    engine.update_doc("doc1", str(md))
    out = json.loads(capsys.readouterr().out)
    assert calls == ["update"]  # no backup: nothing to lose
    assert "consent_note" not in out


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
    # asserted piecewise, not as one literal: the field list grows (author/me
    # joined it for scoped requests), and a substring match on the whole
    # concatenation breaks on every addition without meaning anything
    replies = fields[fields.index("replies("):]
    for f in ("id", "content", "author/displayName", "createdTime"):
        assert f in replies, f
    # the parent's own createdTime, not just the replies' one
    parent = fields[:fields.index("replies(")]
    for f in ("id", "content", "author/displayName", "createdTime"):
        assert f in parent, f


def test_list_comments_carries_a_link_to_each_thread(engine, monkeypatch,
                                                     capsys):
    """#20: naming a thread by id left the person to hunt for it by eye."""
    class _Req:
        def execute(self):
            return {"comments": [{"id": "AAABc", "content": "x"}]}

    class Drive:
        def comments(self):
            return self

        def list(self, **kw):
            return _Req()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: Drive())
    engine.list_comments("doc1")
    out = json.loads(capsys.readouterr().out)
    assert out[0]["link"] == (
        "https://docs.google.com/document/d/doc1/edit?disco=AAABc")


def test_list_comments_always_carries_resolved_and_authorship(engine,
                                                              monkeypatch,
                                                              capsys,
                                                              tmp_path):
    """#41: Drive omits a boolean holding its default, so `resolved` was
    present on one listing and absent from the next — and every consumer
    reading c["resolved"] died on KeyError. Measured live 2026-08-09."""
    class _Req:
        def execute(self):
            return {"comments": [
                # exactly what Drive returned on the second listing: no
                # `resolved` at all, and an author object without `me`
                {"id": "c1", "content": "x",
                 "author": {"displayName": "Заказчик"},
                 "replies": [{"id": "r1", "author": {"displayName": "Автор"}}]},
                {"id": "c2", "content": "y", "resolved": True,
                 "author": {"displayName": "Слава", "me": True}},
            ]}

    class Drive:
        def comments(self):
            return self

        def list(self, **kw):
            return _Req()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: Drive())
    target = tmp_path / "comments.json"
    engine.list_comments("doc1", output=str(target))
    receipt = json.loads(capsys.readouterr().out)
    out = json.loads(target.read_text())
    assert out[0]["resolved"] is False
    assert out[1]["resolved"] is True
    assert out[0]["author"]["me"] is False
    assert out[0]["replies"][0]["author"]["me"] is False
    assert out[1]["author"]["me"] is True
    # «Google did not say» is not «somebody else»: a scoped request has to be
    # checkable against a number instead of being silently narrowed to zero
    assert receipt["authorship_unspecified"] == 2
    assert receipt["mine"] == 1


def test_thread_link_needs_both_ids(engine):
    assert engine._thread_link("doc1", None) is None
    assert engine._thread_link(None, "c1") is None


# --- _write_control fail-closed hardening (codex delta review) ---

def test_write_control_refuses_missing_revision(engine, capsys):
    # the google client drops None from request bodies: an unvalidated None
    # would silently turn a pinned write into an UNPINNED one
    with pytest.raises(SystemExit):
        engine._write_control(None)
    assert "unpinned" in json.loads(capsys.readouterr().out)["error"]


def test_write_control_pins_revision(engine):
    assert engine._write_control("rev7") == {"requiredRevisionId": "rev7"}


def test_update_help_names_the_path_that_keeps_threads(engine, monkeypatch,
                                                       capsys):
    """The live incident of #24: an agent read `--help`, saw the flag, asked
    the person honestly, got a yes — and destroyed 18 threads. The refusal we
    wrote so carefully never reached it. Help is the only channel that does,
    so it has to name the alternative, not just the price."""
    monkeypatch.setattr(sys, "argv", ["skrepka", "update", "--help"])
    with pytest.raises(SystemExit) as ei:
        engine.main()
    assert ei.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "`patch` applies edits and keeps the threads alive" in help_text
    assert "closed threads" in help_text          # the honest caveat
    assert "download" in help_text and "sync" in help_text
    assert "sidecar must stay next to it" in help_text
    assert "not a reason to use this flag" in help_text


def test_destructive_refusal_names_both_shapes_of_the_task(
        engine, monkeypatch, tmp_path, capsys):
    """`patch` for edits, download→sync for a freshly written file — and the
    boundary between them, because sync refuses outright once the new text
    rewrites commented paragraphs. Without the boundary the advice sends the
    agent into a refusal and back here."""
    _stub_update_preflight(monkeypatch, engine, [{"id": "c1"}])
    monkeypatch.setattr(engine, "get_drive_service",
                        lambda c: _fake_update_drive([]))
    md = tmp_path / "doc.md"
    md.write_text("# hi\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        engine.update_doc("doc1", str(md))
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "rewrites a commented fragment whole" in reason
    assert "sidecar must stay beside it" in reason
    assert "those belong to `patch`" in reason
    assert "not a reason to come back here" in reason
    # the order the earlier round pinned: ask the person, THEN the flag
    assert reason.index("ask the person") < reason.index("--acknowledge-loss")


def test_comments_can_tell_whose_thread_it_is(engine, monkeypatch, capsys):
    """«Ответь только на МОИ комментарии» must be answerable from the data.
    Without `author/me` the agent guesses by display name — and a wrong guess
    means writing to the customer, in the customer's document, instead of to
    the person who asked (живой случай 2026-08-09)."""
    captured = {}

    class _Comments:
        def list(self, **kw):
            captured.update(kw)
            return _Result({"comments": [
                {"id": "c1", "content": "мой",
                 "author": {"displayName": "Слава", "me": True}},
                {"id": "c2", "content": "клиента",
                 "author": {"displayName": "Наталья", "me": False}},
            ]})

    class _Result:
        def __init__(self, payload):
            self._p = payload

        def execute(self):
            return self._p

    class _Drive:
        def comments(self):
            return _Comments()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: _Drive())
    engine.list_comments("doc1")

    assert "author/me" in captured["fields"]
    out = json.loads(capsys.readouterr().out)
    assert [c["author"]["me"] for c in out] == [True, False]
