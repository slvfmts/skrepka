"""Component-level partial sync for final protected fences (r15)."""

import json

import pytest

from test_r15 import _html, _main_text_requests, _md, _partial_call
from test_reorder import replay
from test_sync_anchors import (
    CREATED,
    CREATED_SEC,
    DocsStub,
    DriveStub,
    _docx_builder,
    _make_doc_styled,
    _no_content_mutation,
    api_comment,
    make_doc,
    make_workdir,
    wire,
)


A = "Alpha"
P = "Защищённый блок"
B = "Bravo"
C = "Charlie"
TAIL = "Хвост"
TAIL_LOCAL = "Хвост пользователя"


def _named_doc(texts, protected_index):
    doc = make_doc(texts)
    element = doc["body"]["content"][protected_index]
    start = element["startIndex"]
    doc["namedRanges"] = {
        "keep": {"namedRanges": [{"ranges": [{
            "startIndex": start,
            "endIndex": start + len(texts[protected_index]),
        }]}]},
    }
    return doc


def _setup(engine, monkeypatch, tmp_path, *, base, local, safe,
           doc=None, comments=(), anchor_text=None, docx_records=None):
    doc = doc or make_doc(base)
    docs = DocsStub(doc, merged_doc=make_doc(safe, rev="R2"))
    paras = [
        (text, [("0", 0, len(text))]
         if anchor_text is not None and text == anchor_text else [])
        for text in base
    ]
    drive = DriveStub(
        list(comments),
        _docx_builder(docs, paras, docx_records or []),
        html=_html(safe),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))
    return doc, docs, path


def _texts(engine, path):
    parsed, problems = engine._md_elements(
        open(path, encoding="utf-8").read())
    assert not problems
    return [element.get("text") for element in parsed]


def _sidecar_texts(engine, path):
    with open(path + engine.SIDECAR_SUFFIX, encoding="utf-8") as stream:
        payload = json.load(stream)
    return [element.get("text") for element in payload["elements"]]


def test_named_replacement_is_deferred_while_independent_edit_applies(
        engine, monkeypatch, tmp_path, capsys):
    base = [A, P, B, TAIL]
    local = [A, P + " локально", B, TAIL_LOCAL]
    safe = [A, P, B, TAIL_LOCAL]
    doc = _named_doc(base, 1)
    original, docs, path = _setup(
        engine, monkeypatch, tmp_path, base=base, local=local, safe=safe,
        doc=doc)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    assert out["deferred"]["reason"] == "protected-fence"
    assert out["deferred"]["protected_components"][0]["kind"] == "changed"
    assert replay(base, _main_text_requests(docs, original)) == safe
    assert _texts(engine, path) == local
    assert _sidecar_texts(engine, path) == safe


def test_live_anchor_replacement_defers_both_replacement_halves(
        engine, monkeypatch, tmp_path, capsys):
    base = [A, P, B, TAIL]
    local = [A, P + " локально", B, TAIL_LOCAL]
    safe = [A, P, B, TAIL_LOCAL]
    comment = api_comment("c1", "A", CREATED)
    original, docs, path = _setup(
        engine, monkeypatch, tmp_path, base=base, local=local, safe=safe,
        comments=[comment], anchor_text=P,
        docx_records=[("0", "A", CREATED_SEC)])

    out = _partial_call(engine, capsys, "doc1", path)
    requests = _main_text_requests(docs, original)

    assert out["action"] == "partially-synced"
    assert replay(base, requests) == safe
    assert not any(
        request.get("insertText", {}).get("text") == P + " локально"
        for request in requests)
    protected = original["body"]["content"][1]
    for request in requests:
        removed = request.get("deleteContentRange", {}).get("range")
        if removed:
            assert not (
                removed["startIndex"] < protected["endIndex"]
                and protected["startIndex"] < removed["endIndex"])


def test_named_delete_uses_an_empty_intent_hull_without_resurrection(
        engine, monkeypatch, tmp_path, capsys):
    base = [A, P, B, TAIL]
    local = [A, B, TAIL_LOCAL]
    safe = [A, P, B, TAIL_LOCAL]
    doc = _named_doc(base, 1)
    original, docs, path = _setup(
        engine, monkeypatch, tmp_path, base=base, local=local, safe=safe,
        doc=doc)

    out = _partial_call(engine, capsys, "doc1", path)

    component = out["deferred"]["protected_components"][0]
    assert component["kind"] == "deleted"
    assert component["local_hull"][0] > component["local_hull"][1]
    assert replay(base, _main_text_requests(docs, original)) == safe
    assert _texts(engine, path) == local
    assert _sidecar_texts(engine, path) == safe


def test_protected_move_defers_the_whole_corridor_and_applies_tail(
        engine, monkeypatch, tmp_path, capsys):
    delimiter = "Неподвижная граница"
    base = [A, P, B, C, delimiter, TAIL]
    local = [A, B, C, P, delimiter, TAIL_LOCAL]
    safe = [A, P, B, C, delimiter, TAIL_LOCAL]
    doc = _named_doc(base, 1)
    original, docs, path = _setup(
        engine, monkeypatch, tmp_path, base=base, local=local, safe=safe,
        doc=doc)

    out = _partial_call(engine, capsys, "doc1", path)
    requests = _main_text_requests(docs, original)

    component = out["deferred"]["protected_components"][0]
    assert component["kind"] == "reorder"
    assert component["base_hull"] == [1, 3]
    assert replay(base, requests) == safe
    assert not any(
        P in request.get("insertText", {}).get("text", "")
        for request in requests)
    assert _texts(engine, path) == local


def test_insert_on_protected_reorder_boundary_is_deferred_with_corridor(
        engine, monkeypatch, tmp_path, capsys):
    delimiter = "Неподвижная граница"
    boundary_insert = "Вставка на общей границе"
    base = [A, P, B, C, delimiter, TAIL]
    local = [A, B, C, P, boundary_insert, delimiter, TAIL_LOCAL]
    safe = [A, P, B, C, delimiter, TAIL_LOCAL]
    doc = _named_doc(base, 1)
    original, docs, path = _setup(
        engine, monkeypatch, tmp_path, base=base, local=local, safe=safe,
        doc=doc)

    out = _partial_call(engine, capsys, "doc1", path)
    requests = _main_text_requests(docs, original)

    component = out["deferred"]["protected_components"][0]
    assert component["kind"] == "reorder"
    assert component["base_hull"] == [1, 3]
    assert replay(base, requests) == safe
    assert not any(
        boundary_insert in request.get("insertText", {}).get("text", "")
        for request in requests)
    assert _texts(engine, path) == local


def test_remote_dirty_protected_component_remains_a_global_conflict(
        engine, monkeypatch, tmp_path, capsys):
    base = [A, P, B, TAIL]
    local = [A, P + " локально", B, TAIL_LOCAL]
    remote = [A, P + " коллеги", B, TAIL]
    doc = _named_doc(base, 1)
    live = _named_doc(remote, 1)
    docs = DocsStub(live)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, _md(base), _md(local))

    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", path)
    out = json.loads(capsys.readouterr().out)

    assert "not remote-clean" in out["error"]
    assert _no_content_mutation(docs)


def test_ambiguous_anchor_blocked_fence_is_deferred_locally(
        engine, monkeypatch, tmp_path, capsys):
    base_md = f"# {B}\n\n{B}\n\n{TAIL}"
    local_md = f"# {B} edited\n\n{B}\n\n{TAIL_LOCAL}"
    doc = _make_doc_styled([
        (B, "HEADING_1"), (B, "NORMAL_TEXT"),
        (TAIL, "NORMAL_TEXT")])
    safe_doc = _make_doc_styled([
        (B, "HEADING_1"), (B, "NORMAL_TEXT"),
        (TAIL_LOCAL, "NORMAL_TEXT")], rev="R2")
    docs = DocsStub(doc, merged_doc=safe_doc)
    comment = api_comment("c1", "A", CREATED)
    # The marker text has two API-side candidates (heading/body), so the
    # accounting cannot choose one and fences both exact ranges in `blocked`.
    drive = DriveStub(
        [comment],
        _docx_builder(
            docs, [(B, []), (B, [("0", 0, len(B))]), (TAIL, [])],
            [("0", "A", CREATED_SEC)]),
        html=(f"<h1>{B}</h1><p>{B}</p><p>{TAIL_LOCAL}</p>").encode(),
    )
    wire(engine, monkeypatch, docs, drive)
    path = make_workdir(engine, tmp_path, doc, base_md, local_md)

    out = _partial_call(engine, capsys, "doc1", path)

    assert out["action"] == "partially-synced"
    fences = out["deferred"]["protected_components"][0]["fences"]
    assert any("sha256:" in fence for fence in fences)
    assert _texts(engine, path) == [B + " edited", B, TAIL_LOCAL]
