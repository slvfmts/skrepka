"""Issue #44: read-only comment listings carry honest tab attribution."""

import copy
import json

import pytest


class _Result:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return copy.deepcopy(self.payload)


def _body(text):
    end = 1 + len(text)
    return {"body": {"content": [{
        "startIndex": 1,
        "endIndex": end,
        "paragraph": {"elements": [{
            "startIndex": 1,
            "endIndex": end,
            "textRun": {"content": text},
        }]},
    }]}}


def _tab(tab_id, title, text, *, children=()):
    tab = {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": _body(text),
    }
    if children:
        tab["childTabs"] = list(children)
    return tab


def _comment(comment_id, quote=None, anchor=None):
    comment = {
        "id": comment_id,
        "content": comment_id,
        "author": {"displayName": "Автор", "me": True},
        "replies": [],
    }
    if quote is not None:
        comment["quotedFileContent"] = {"value": quote}
    if anchor is not None:
        comment["anchor"] = anchor
    return comment


class _Docs:
    def __init__(self, doc):
        self.doc = copy.deepcopy(doc)
        self.doc.setdefault("revisionId", "R1")
        self.get_calls = []
        self.batch_calls = []

    def documents(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        if self.doc.get("tabs") and kwargs.get("includeTabsContent") is not True:
            # Reproduce the dangerous M19 default: child/sibling tabs vanish.
            first = self.doc["tabs"][0]
            return _Result({
                "title": self.doc.get("title", ""),
                **first.get("documentTab", {}),
            })
        return _Result(self.doc)

    def batchUpdate(self, **kwargs):  # pragma: no cover - assertion is the test
        self.batch_calls.append(dict(kwargs))
        raise AssertionError("comments must not write a canary")


class _Drive:
    def __init__(self, comments):
        self.comment_rows = comments
        self.list_calls = []
        self.export_calls = []

    def comments(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        return _Result({"comments": self.comment_rows})

    def files(self):
        self.export_calls.append("files")
        # #37 deliberately adds a read-only export. This stub keeps #44
        # focused on tab attribution while also proving export failure does
        # not erase a valid Docs-side attribution.
        raise RuntimeError("DOCX unavailable in tab-attribution fixture")


def _wire(engine, monkeypatch, doc, comments):
    docs = _Docs(doc)
    drive = _Drive(comments)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _creds: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: drive)
    return docs, drive


def test_nested_tabs_unique_multiple_zero_and_document_shapes(
        engine, monkeypatch, capsys, tmp_path):
    child = _tab(
        "t-child", "Приложение",
        "unique child\nshared quote\n")
    doc = {
        "title": "Документ",
        "tabs": [
            _tab("t-root", "Черновик",
                 "twice here / twice here\nshared quote\n",
                 children=(child,)),
            _tab("t-other", "Чистовик", "unrelated\n"),
        ],
    }
    comments = [
        _comment("unique", "unique child", "opaque-anchor"),
        # Several occurrences in ONE body still identify exactly one tab.
        _comment("same-tab-twice", "twice here", "opaque-anchor"),
        _comment("multiple-tabs", "shared quote", "opaque-anchor"),
        _comment("zero-tabs", "old deleted text", "opaque-anchor"),
        _comment("document"),
        _comment("anchor-no-quote", anchor="opaque-anchor"),
    ]
    docs, drive = _wire(engine, monkeypatch, doc, comments)
    target = tmp_path / "comments.json"

    engine.list_comments("doc1", output=str(target))

    rows = {row["id"]: row for row in json.loads(target.read_text())}
    receipt = json.loads(capsys.readouterr().out)

    assert rows["unique"]["tab_id"] == "t-child"
    assert rows["unique"]["tab_title"] == "Приложение"
    assert rows["unique"]["tab_attribution"] == {
        "status": "exact",
        "candidates": [{
            "tab_id": "t-child",
            "tab_title": "Приложение",
            "quote_occurrences": 1,
        }],
        "reason": "quote_matches_exactly_one_tab",
    }

    twice = rows["same-tab-twice"]
    assert twice["tab_id"] == "t-root"
    assert twice["tab_attribution"]["status"] == "exact"
    assert twice["tab_attribution"]["candidates"][0][
        "quote_occurrences"] == 2

    multiple = rows["multiple-tabs"]
    assert multiple["tab_id"] is None
    assert multiple["tab_title"] is None
    assert multiple["tab_attribution"]["status"] == "unknown"
    assert multiple["tab_attribution"]["reason"] == (
        "quote_matches_multiple_tabs")
    assert [c["tab_id"] for c in multiple["tab_attribution"]["candidates"]] == [
        "t-root", "t-child"]

    zero = rows["zero-tabs"]
    assert zero["tab_id"] is None
    assert zero["tab_attribution"] == {
        "status": "unknown",
        "candidates": [],
        "reason": "quote_not_found_in_tabs",
    }
    assert rows["document"]["tab_attribution"] == {
        "status": "document",
        "candidates": [],
        "reason": "unanchored_document_comment",
    }
    assert rows["anchor-no-quote"]["tab_attribution"] == {
        "status": "unknown",
        "candidates": [],
        "reason": "anchor_without_quote",
    }

    assert receipt["comments"] == 6
    assert receipt["tab_exact"] == 2
    assert receipt["tab_unknown"] == 3
    assert receipt["document_level"] == 1
    assert len(docs.get_calls) == 2
    assert docs.get_calls[0]["includeTabsContent"] is True
    assert docs.get_calls[0]["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    assert docs.batch_calls == []
    assert drive.export_calls == ["files"]
    assert "anchor" in drive.list_calls[0]["fields"]


@pytest.mark.parametrize(
    ("bad_ids", "reason"),
    [(("duplicate", "duplicate"), "duplicate_tab_id"),
     (("t-root", None), "missing_tab_id")],
)
def test_duplicate_or_missing_tab_ids_never_produce_exact_attribution(
        engine, monkeypatch, capsys, bad_ids, reason):
    child = _tab(bad_ids[1], "Дочерняя", "different text\n")
    doc = {"tabs": [
        _tab(bad_ids[0], "Корневая", "only match\n", children=(child,)),
    ]}
    _docs, _drive = _wire(
        engine, monkeypatch, doc,
        [_comment("c1", "only match", "opaque-anchor")])

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_id"] is None
    assert row["tab_attribution"]["status"] == "unknown"
    assert row["tab_attribution"]["reason"] == reason
    # The observed text match is still reported as a candidate, but is never
    # promoted through a broken identity catalogue.
    assert row["tab_attribution"]["candidates"][0]["tab_title"] == "Корневая"


def test_legacy_body_without_tab_ids_is_a_candidate_not_an_exact_tab(
        engine, monkeypatch, capsys):
    doc = {"title": "Старый ответ API", **_body("quoted text\n")}
    _docs, _drive = _wire(
        engine, monkeypatch, doc,
        [_comment("c1", "quoted text", "opaque-anchor")])

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_id"] is None
    assert row["tab_attribution"]["status"] == "unknown"
    assert row["tab_attribution"]["reason"] == "tab_ids_not_returned"
    assert row["tab_attribution"]["candidates"] == [{
        "tab_id": None,
        "tab_title": "Старый ответ API",
        "quote_occurrences": 1,
    }]


def test_docs_read_failure_keeps_comments_but_marks_quote_unknown(
        engine, monkeypatch, capsys):
    drive = _Drive([
        _comment("quoted", "some quote", "opaque-anchor"),
        _comment("document"),
    ])
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: drive)
    monkeypatch.setattr(
        engine, "get_docs_service",
        lambda _creds: (_ for _ in ()).throw(RuntimeError("Docs offline")))

    engine.list_comments("doc1")

    captured = capsys.readouterr()
    rows = {row["id"]: row for row in json.loads(captured.out)}
    assert rows["quoted"]["tab_id"] is None
    assert rows["quoted"]["tab_attribution"]["status"] == "unknown"
    assert rows["quoted"]["tab_attribution"]["reason"] == (
        "document_tabs_unavailable")
    assert rows["document"]["tab_attribution"]["status"] == "document"
    assert "Docs offline" in captured.err
