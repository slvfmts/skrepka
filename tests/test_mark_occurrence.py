"""#35a: omitted and explicit occurrence are different contracts."""

import sys

import pytest


def test_mark_occurrence_resolver_distinguishes_omitted_from_explicit_one(engine):
    resolve = engine._resolve_mark_occurrence
    assert resolve(None, 1) == 1
    with pytest.raises(ValueError, match="non-unique"):
        resolve(None, 2)
    assert resolve(1, 2) == 1
    assert resolve(2, 2) == 2


@pytest.mark.parametrize("requested", [0, -1, 3])
def test_mark_occurrence_resolver_rejects_bounds(engine, requested):
    with pytest.raises(ValueError, match="out of range"):
        engine._resolve_mark_occurrence(requested, 2)


def test_mark_omitted_duplicate_refuses_before_batch_update(engine, monkeypatch,
                                                            capsys):
    from test_tabs import _tab

    doc = {"revisionId": "R0", "tabs": [_tab("t.0", "Main", "repeat\nrepeat")]}

    class Docs:
        def get(self, **_kw):
            return type("R", (), {"execute": lambda self: doc})()

        def documents(self):
            return self

        def batchUpdate(self, **_kw):  # pragma: no cover - tripwire
            raise AssertionError("mark must refuse before write")

    class Drive:
        pass

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _c: Docs())
    monkeypatch.setattr(engine, "get_drive_service", lambda _c: Drive())
    with pytest.raises(SystemExit) as exc:
        engine.mark_range("F", "n", "repeat")
    assert exc.value.code == 1
    assert "--occurrence" in capsys.readouterr().out


def test_mark_explicit_one_targets_first_duplicate_and_receipt(engine, monkeypatch,
                                                               capsys):
    from test_tabs import _tab

    doc = {"revisionId": "R0", "tabs": [_tab("t.0", "Main", "repeat\nrepeat")]}

    class Docs:
        def __init__(self):
            self.batch = None

        def get(self, **_kw):
            return type("R", (), {"execute": lambda self: doc})()

        def documents(self):
            return self

        def batchUpdate(self, **kw):
            self.batch = kw
            return type("R", (), {"execute": lambda self: {"replies": [{
                "createNamedRange": {"namedRangeId": "nr"}}]}})()

    docs = Docs()
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _c: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _c: object())
    engine.mark_range("F", "n", "repeat", occurrence=1)
    out = capsys.readouterr().out
    assert '"occurrence": 1' in out
    req = docs.batch["body"]["requests"][0]["createNamedRange"]
    assert req["range"]["startIndex"] == 1


def test_cli_omits_occurrence_as_none(engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, "mark_range",
                        lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(sys, "argv", ["skrepka", "mark", "F", "n",
                                        "--quote", "q"])
    engine.main()
    assert seen["occurrence"] is None

