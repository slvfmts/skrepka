"""Suggestions must be read and reported per Google Docs tab.

M19 measured the dangerous default precisely: without
``includeTabsContent=True`` a ``documents.get`` response contains only the
FIRST tab in the legacy top-level ``body`` and an empty ``tabs`` field.  The
stub below reproduces that behavior instead of returning the full fixture no
matter which arguments production sent.  This makes the two request-flag
assertions behavioral: removing either flag hides a real suggestion.
"""

import copy
import json

import pytest


WITHOUT = "PREVIEW_WITHOUT_SUGGESTIONS"
ACCEPTED = "PREVIEW_SUGGESTIONS_ACCEPTED"


class _Result:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return copy.deepcopy(self.value)


class SuggestionViews:
    """Docs service with separate without/accepted views and M19 defaults."""

    def __init__(self, without, accepted):
        self.views = {WITHOUT: without, ACCEPTED: accepted}
        self.calls = []

    def documents(self):
        return self

    def get(self, **kwargs):
        self.calls.append(dict(kwargs))
        doc = self.views[kwargs["suggestionsViewMode"]]
        if doc.get("tabs") and kwargs.get("includeTabsContent") is not True:
            # M19-6: Google exposes only the first ROOT tab as a legacy body;
            # children and neighbouring roots are invisible.
            first = doc["tabs"][0]
            doc = {"title": doc.get("title", ""),
                   **copy.deepcopy(first.get("documentTab", {}))}
        return _Result(doc)

    def batchUpdate(self, **_kwargs):
        raise AssertionError("suggestions is a read-only command")


def _body(*lines):
    content = []
    start = 1
    for line in lines:
        text = line + "\n"
        end = start + len(text)
        content.append({
            "startIndex": start,
            "endIndex": end,
            "paragraph": {"elements": [{
                "startIndex": start,
                "endIndex": end,
                "textRun": {"content": text, "textStyle": {}},
            }]},
        })
        start = end
    return {"body": {"content": content}}


def _tab(tab_id, title, *lines, children=()):
    tab = {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": _body(*lines),
    }
    if children:
        tab["childTabs"] = list(children)
    return tab


def _doc(*tabs, title="Документ с предложениями"):
    return {"title": title, "tabs": list(tabs)}


def _legacy(*lines, title="Старый документ"):
    return {"title": title, **_body(*lines)}


def _wire(engine, monkeypatch, without, accepted):
    service = SuggestionViews(without, accepted)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _creds: service)
    return service


def _read(engine, monkeypatch, capsys, without, accepted):
    service = _wire(engine, monkeypatch, without, accepted)
    engine.list_suggestions("doc1")
    return json.loads(capsys.readouterr().out), service


def _multi_views(*, accepted_root_order=False):
    child_before = _tab("t.child", "Приложение", "child old")
    child_after = _tab("t.child", "Приложение", "child new")
    root_a_before = _tab("t.a", "Черновик", "unchanged",
                         children=(child_before,))
    root_a_after = _tab("t.a", "Черновик", "unchanged",
                        children=(child_after,))
    root_b_before = _tab("t.b", "Чистовик", "root old")
    root_b_after = _tab("t.b", "Чистовик", "root new")
    before = _doc(root_a_before, root_b_before)
    after_tabs = ((root_b_after, root_a_after) if accepted_root_order
                  else (root_a_after, root_b_after))
    return before, _doc(*after_tabs)


def test_both_preview_reads_include_all_tabs_and_find_root_and_child_changes(
        engine, monkeypatch, capsys):
    before, after = _multi_views()
    out, service = _read(engine, monkeypatch, capsys, before, after)

    assert len(service.calls) == 2
    assert {call["suggestionsViewMode"] for call in service.calls} == {
        WITHOUT, ACCEPTED}
    assert all(call.get("includeTabsContent") is True
               for call in service.calls), service.calls

    assert out["has_suggestions"] is True
    assert out["tab_count"] == 3
    assert out["tabs_with_suggestions"] == 2
    assert out["change_count"] == 2
    tabs = {tab["id"]: tab for tab in out["tabs"]}
    assert set(tabs) == {"t.a", "t.child", "t.b"}
    assert tabs["t.a"]["has_suggestions"] is False
    assert tabs["t.child"]["has_suggestions"] is True
    assert tabs["t.b"]["has_suggestions"] is True

    changes = {change["tab_id"]: change for change in out["changes"]}
    assert changes["t.child"]["tab_title"] == "Приложение"
    assert changes["t.child"]["deleted"] == ["child old"]
    assert changes["t.child"]["inserted"] == ["child new"]
    assert changes["t.b"]["tab_title"] == "Чистовик"
    assert changes["t.b"]["deleted"] == ["root old"]
    assert changes["t.b"]["inserted"] == ["root new"]
    # The human-readable diff must retain the same attribution; otherwise an
    # agent reading `diff` instead of `changes` can apply advice to a twin tab.
    assert "t.child" in out["diff"] and "Приложение" in out["diff"]
    assert "t.b" in out["diff"] and "Чистовик" in out["diff"]


def test_views_are_matched_by_tab_id_not_by_response_order(
        engine, monkeypatch, capsys):
    before, after = _multi_views(accepted_root_order=True)
    out, _service = _read(engine, monkeypatch, capsys, before, after)

    changes = {change["tab_id"]: change for change in out["changes"]}
    assert changes["t.b"]["deleted"] == ["root old"]
    assert changes["t.b"]["inserted"] == ["root new"]
    assert changes["t.child"]["deleted"] == ["child old"]
    assert changes["t.child"]["inserted"] == ["child new"]
    assert "t.a" not in changes


def test_a_tab_missing_from_one_preview_is_an_explicit_retryable_error(
        engine, monkeypatch, capsys):
    before, after = _multi_views()
    # The accepted response lost the child while retaining both root tabs.
    after["tabs"][0].pop("childTabs")
    _wire(engine, monkeypatch, before, after)

    with pytest.raises(SystemExit):
        engine.list_suggestions("doc1")
    error = json.loads(capsys.readouterr().out)["error"]
    assert "suggestion" in error.lower()
    assert "tab" in error.lower()
    assert "t.child" in error
    assert "retry" in error.lower()


def test_duplicate_tab_ids_are_an_explicit_error(
        engine, monkeypatch, capsys):
    before = _doc(_tab("t.a", "Черновик", "old"),
                  _tab("t.b", "Чистовик", "same"))
    after = _doc(_tab("t.a", "Черновик", "new"),
                 _tab("t.a", "Дубликат", "same"))
    _wire(engine, monkeypatch, before, after)

    with pytest.raises(SystemExit):
        engine.list_suggestions("doc1")
    error = json.loads(capsys.readouterr().out)["error"]
    assert "duplicate" in error.lower()
    assert "tab" in error.lower()
    assert "t.a" in error


def test_legacy_single_tab_document_keeps_working(
        engine, monkeypatch, capsys):
    out, service = _read(
        engine, monkeypatch, capsys,
        _legacy("old line"), _legacy("new line"))

    assert all(call.get("includeTabsContent") is True
               for call in service.calls)
    assert out["has_suggestions"] is True
    assert out["tab_count"] == 1
    assert out["tabs_with_suggestions"] == 1
    assert out["change_count"] == 1
    assert out["tabs"] == [{
        "id": None,
        "title": "Старый документ",
        "has_suggestions": True,
        "change_count": 1,
    }]
    assert out["changes"][0]["tab_id"] is None
    assert out["changes"][0]["tab_title"] == "Старый документ"
    assert out["changes"][0]["deleted"] == ["old line"]
    assert out["changes"][0]["inserted"] == ["new line"]


def test_multitab_noop_reports_every_tab_scanned(
        engine, monkeypatch, capsys):
    child = _tab("t.child", "Приложение", "same child")
    doc = _doc(_tab("t.a", "Черновик", "same", children=(child,)),
               _tab("t.b", "Чистовик", "same root"))
    out, _service = _read(engine, monkeypatch, capsys, doc, doc)

    assert out["has_suggestions"] is False
    assert out["change_count"] == 0
    assert out["changes"] == []
    assert out["diff"] == ""
    assert out["tab_count"] == 3
    assert out["tabs_with_suggestions"] == 0
    assert [tab["id"] for tab in out["tabs"]] == [
        "t.a", "t.child", "t.b"]
    assert all(tab["has_suggestions"] is False for tab in out["tabs"])


def test_output_file_contains_tab_aware_payload_and_receipt_stays_short(
        engine, monkeypatch, capsys, tmp_path):
    before, after = _multi_views()
    service = _wire(engine, monkeypatch, before, after)
    target = tmp_path / "suggestions.json"

    engine.list_suggestions("doc1", output=str(target))

    payload = json.loads(target.read_text(encoding="utf-8"))
    receipt = json.loads(capsys.readouterr().out)
    assert payload["tab_count"] == 3
    assert payload["tabs_with_suggestions"] == 2
    assert {change["tab_id"] for change in payload["changes"]} == {
        "t.child", "t.b"}
    assert receipt["written"] == str(target)
    assert receipt["bytes"] == target.stat().st_size
    assert receipt["has_suggestions"] is True
    assert receipt["change_count"] == 2
    assert receipt["tab_count"] == 3
    assert receipt["tabs_with_suggestions"] == 2
    assert "changes" not in receipt and "tabs" not in receipt
    assert all(call.get("includeTabsContent") is True
               for call in service.calls)
